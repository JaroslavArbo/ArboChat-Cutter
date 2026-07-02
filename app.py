
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import json
import base64, os, re, shutil, subprocess, csv, mimetypes, urllib.parse, uuid, datetime, html, wave, math, struct, base64, hashlib
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
PROJECTS = ROOT / "projects"
CONFIG = ROOT / "config"
SETTINGS_FILE = CONFIG / "settings.json"
TERMS_FILE = CONFIG / "internal_terms.json"

PROJECTS.mkdir(exist_ok=True)
CONFIG.mkdir(exist_ok=True)

ROLE_FILES = {
    "speaker_video": "speaker.mp4",
    "gallery_video": "gallery.mp4",
    "replacement_video": "replacement.mp4",
    "transcript_file": "transcript.vtt",
}

COMMON_FILES = {
    "topics_file": "topics.csv",
    "intro_template": "intro.mp4",
    "outro_template": "outro.mp4",
}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv"}
TEXT_EXTS = {".vtt", ".srt", ".txt", ".json"}
TOPIC_EXTS = {".csv", ".xlsx"}

def load_settings():
    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    default_intro = CONFIG / "intro.mp4"
    default_outro = CONFIG / "outro.mp4"
    if not data.get("intro_template") and default_intro.exists():
        data["intro_template"] = str(default_intro)
    if not data.get("outro_template") and default_outro.exists():
        data["outro_template"] = str(default_outro)
    return data

def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def intro_layout_defaults():
    settings = load_settings()
    layout = settings.get("intro_layout_defaults")
    return layout if isinstance(layout, dict) else {}

def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9_. -]+", "_", name.strip())
    name = name.replace(" ", "_")
    return name or "project"


def extract_project_date(name):
    text = str(name or "")
    m = re.search(r"(20\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?([0-3]\d)", text)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            pass
    m = re.search(r"([0-3]\d)[-_. ](0[1-9]|1[0-2])[-_. ](20\d{2})", text)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            pass
    return ""

def project_dir(pid):
    return PROJECTS / pid

def project_config(pid):
    p = project_dir(pid) / "project.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"id": pid, "name": pid, "files": {}, "meta": {}, "cuts": {}, "analysis": {}}

def save_project(cfg):
    d = project_dir(cfg["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

_FILTER_CACHE = {}

def ffmpeg_has_filter(name):
    if name in _FILTER_CACHE:
        return _FILTER_CACHE[name]
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = (p.stdout or "") + (p.stderr or "")
    except Exception:
        data = ""
    result = name in data
    _FILTER_CACHE[name] = result
    return result

_HWENC_CACHE = {}

def hw_video_encoder():
    """Return ('h264_videotoolbox', extra_args) if usable on this machine, else None.

    Cached for the process lifetime: the result depends only on the local ffmpeg
    build/hardware, not on any per-call state.
    """
    if "videotoolbox" not in _HWENC_CACHE:
        usable = False
        try:
            p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            data = (p.stdout or "") + (p.stderr or "")
            usable = "h264_videotoolbox" in data
        except Exception:
            usable = False
        _HWENC_CACHE["videotoolbox"] = usable
    return _HWENC_CACHE["videotoolbox"]

def video_encode_args(crf="20", vt_bitrate="9M"):
    """Video encoder args shared by every segment-building helper.

    Prefers the macOS hardware encoder (several times faster than software x264
    for 1080p30 and plenty good enough for a cut/trim tool), falling back to the
    previous libx264 settings whenever videotoolbox is unavailable so behavior on
    non-mac builds/ffmpeg without it is unchanged. `vt_bitrate` should be lowered
    by callers encoding at less than Full HD (e.g. small previews) so hardware
    mode doesn't waste bitrate/time on a low-resolution frame.
    """
    if hw_video_encoder():
        # videotoolbox has no CRF; approximate visual quality with a bitrate
        # ceiling similar to what the equivalent libx264 crf produced.
        value = "".join(ch for ch in str(vt_bitrate) if ch.isdigit()) or "9"
        unit = "".join(ch for ch in str(vt_bitrate) if ch.isalpha()) or "M"
        base = int(value)
        return ["-c:v", "h264_videotoolbox", "-b:v", f"{base}{unit}", "-maxrate", f"{int(base*1.35)}{unit}", "-bufsize", f"{base*2}{unit}"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]

_PROBE_CACHE = {}

def ffprobe(path):
    """ffprobe with an in-process cache keyed by (path, mtime, size).

    The same intro/outro/speaker/gallery/replacement files get probed many times
    across one compose+render pass; the underlying file cannot change mid-run, so
    caching removes dozens of redundant subprocess spawns per export with no
    behavior change.
    """
    key = str(path)
    try:
        st = os.stat(key)
        cache_key = (key, st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _PROBE_CACHE:
        return _PROBE_CACHE[cache_key]
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    code, out, err = run(cmd)
    if code != 0:
        return {"error": err[-1200:]}
    try:
        data = json.loads(out)
    except Exception as e:
        return {"error": str(e)}
    info = {}
    fmt = data.get("format", {})
    try: info["duration"] = float(fmt.get("duration", 0))
    except Exception: info["duration"] = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and "video" not in info:
            info["video"] = {
                "codec": s.get("codec_name"),
                "width": s.get("width"),
                "height": s.get("height"),
                "fps": s.get("avg_frame_rate") or s.get("r_frame_rate")
            }
            # Keep width/height also at top level for older UI/debug code.
            info["width"] = s.get("width")
            info["height"] = s.get("height")
        if s.get("codec_type") == "audio" and "audio" not in info:
            info["audio"] = {
                "codec": s.get("codec_name"),
                "channels": s.get("channels"),
                "sample_rate": s.get("sample_rate")
            }
    if cache_key is not None:
        _PROBE_CACHE[cache_key] = info
    return info

def _is_full_hd_h264_aac(info):
    """True if an ffprobe() result already matches the app's Full HD render spec.

    Used to skip a redundant re-encode when a file (e.g. the edit master) was
    already produced by this app's own normalization pipeline.
    """
    video = info.get("video") or {}
    audio = info.get("audio") or {}
    try:
        if str(video.get("codec") or "") != "h264":
            return False
        if int(video.get("width") or 0) != 1920 or int(video.get("height") or 0) != 1080:
            return False
        if str(audio.get("codec") or "") != "aac":
            return False
    except Exception:
        return False
    return True

def parse_time_to_seconds(s):
    if not s: return None
    s = str(s).strip().replace(",", ".")
    m = re.match(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$", s)
    if m:
        h = int(m.group(1) or 0); mi = int(m.group(2)); sec = float(m.group(3))
        return h*3600+mi*60+sec
    try:
        return float(s)
    except Exception:
        return None

def seconds_to_time(t):
    t = max(0, float(t or 0))
    h = int(t//3600); m = int((t%3600)//60); s = t%60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

def read_text_any(path):
    if not path or not Path(path).exists():
        return ""
    p = Path(path)
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(txt)
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return txt
    return txt

def parse_transcript(path):
    txt = read_text_any(path)
    entries = []
    if not txt:
        return entries, ""
    # VTT/SRT timing lines
    lines = txt.splitlines()
    for i, line in enumerate(lines):
        if "-->" in line:
            left, right = line.split("-->", 1)
            start = parse_time_to_seconds(left.strip())
            end = parse_time_to_seconds(right.strip().split()[0])
            words = []
            j = i + 1
            while j < len(lines) and lines[j].strip():
                if "-->" not in lines[j]:
                    words.append(re.sub(r"<[^>]+>", "", lines[j].strip()))
                j += 1
            text = " ".join(words).strip()
            if text:
                entries.append({"start": start or 0, "end": end or (start or 0)+3, "text": text})
    if not entries:
        plain = re.sub(r"<[^>]+>", "", txt)
        entries.append({"start": 0, "end": 0, "text": plain})
    plain = " ".join(e["text"] for e in entries)
    return entries, plain

def find_phrase_entries(entries, patterns, label, kind="phrase"):
    found = []
    for e in entries:
        text = e["text"]
        low = text.lower()
        for pat in patterns:
            if re.search(pat, low, flags=re.I):
                found.append({
                    "label": label,
                    "kind": kind,
                    "start": e.get("start", 0),
                    "end": e.get("end", e.get("start", 0)+3),
                    "text": text[:240]
                })
                break
    return found

def load_terms():
    try:
        return json.loads(TERMS_FILE.read_text(encoding="utf-8")).get("terms", [])
    except Exception:
        return []

def detect_terms(entries):
    out = []
    for term in load_terms():
        action = term.get("action", "mark")
        label = term.get("label", "")
        for e in entries:
            low = e["text"].lower()
            for v in term.get("variants", []):
                if v.lower() in low:
                    out.append({
                        "label": label,
                        "kind": "term_cut" if action == "suggest_cut" else "term",
                        "action": action,
                        "start": max(0, e.get("start", 0)-5 if action=="suggest_cut" else e.get("start", 0)),
                        "end": (e.get("end", e.get("start", 0)+3)+5 if action=="suggest_cut" else e.get("end", e.get("start", 0)+3)),
                        "text": e["text"][:240]
                    })
                    break
    return out

def normalize_header(h):
    s = str(h or "").strip().lower()
    table = str.maketrans({
        "á":"a","č":"c","ď":"d","é":"e","ě":"e","í":"i","ň":"n","ó":"o","ř":"r","š":"s","ť":"t","ú":"u","ů":"u","ý":"y","ž":"z"
    })
    s = s.translate(table)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    aliases = {
        "cislo": "episode_number",
        "cislo_dilu": "episode_number",
        "dil": "episode_number",
        "epizoda": "episode_number",
        "datum": "date",
        "datum_konani": "date",
        "termin": "date",
        "tema": "topic",
        "nazev": "topic",
        "nazev_tematu": "topic",
        "nazev_arbochatu": "topic",
        "arbochat": "topic",
        "recnik": "speaker",
        "prednasejici": "speaker",
        "host": "speaker",
        "lektor": "speaker",
        "jmeno": "speaker",
        "jmeno_recnik": "speaker",
        "jmeno_recnika": "speaker",
    }
    return aliases.get(s, s)

def read_topics_csv(path):
    rows = []
    if not path or not Path(path).exists():
        return rows
    p = Path(path)

    if p.suffix.lower() == ".xlsx":
        return rows

    try:
        raw = p.read_text(encoding="utf-8-sig", errors="ignore")
        sample = raw[:5000]

        delimiter = ";"
        if sample.count(",") > sample.count(";"):
            delimiter = ","
        if sample.count("\t") > sample.count(delimiter):
            delimiter = "\t"

        import csv
        reader = csv.DictReader(raw.splitlines(), delimiter=delimiter)

        for row in reader:
            norm = {}
            for k, v in row.items():
                if k is None:
                    continue
                nk = normalize_header(k)
                norm[nk] = (v or "").strip()
            if any(norm.values()):
                rows.append(norm)

    except Exception as e:
        print("CSV read failed:", e)

    return rows


def guess_date_from_files(cfg):
    parts = [Path(v).name for v in cfg.get("files", {}).values() if isinstance(v, str)]
    source_folder = cfg.get("source", {}).get("folder")
    if source_folder:
        parts.append(Path(source_folder).name)
    names = " ".join(parts)
    # GMT20260601, 20260601, 2026-06-01, 2026_06_01
    for pat in [r"(20\d{2})(\d{2})(\d{2})", r"(20\d{2})[-_.](\d{2})[-_.](\d{2})"]:
        m = re.search(pat, names)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""

def extract_metadata_from_transcript(entries, plain):
    meta = {}
    text = re.sub(r"\s+", " ", plain[:12000]).strip()

    topic_patterns = [
        r"(?:dnešní(?:m)? tématem je|dnes(?:ka)? (?:si )?budeme (?:povídat|mluvit) o|téma dnešního arbochatu je|tématem dnešního setkání je)\s+(.{5,150}?)(?:\.|\?|!|, naš|, s| a naš| a s)",
        r"(?:téma je|tématem je)\s+(.{5,150}?)(?:\.|\?|!)",
    ]

    for pat in topic_patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            meta["topic"] = m.group(1).strip(" .,:;")
            break

    speaker_patterns = [
        r"(?:naším hostem je|naše pozvání přijal(?:a)?|představím vám|řečníkem je|dnes je s námi|dneska je s námi)\s+(.{5,100}?)(?:\.|\?|!|, který|, která| a\b)",
        r"(?:s panem|s paní)\s+(.{5,90}?)(?:\.|\?|!|,)",
    ]

    for pat in speaker_patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            meta["speaker"] = m.group(1).strip(" .,:;")
            break

    return meta


def match_topics(cfg, transcript_meta):
    settings = load_settings()
    candidate_paths = []
    if settings.get("topics_file"):
        candidate_paths.append(settings.get("topics_file"))
    # Fallback: CSV in the source folder, useful when the plan sits next to Zoom files.
    source_folder = cfg.get("source", {}).get("folder")
    if source_folder and Path(source_folder).exists():
        for p in Path(source_folder).iterdir():
            if p.is_file() and p.suffix.lower() == ".csv":
                candidate_paths.append(str(p))
    rows = []
    used_path = ""
    for path in candidate_paths:
        rows = read_topics_csv(path)
        if rows:
            used_path = path
            break
    if not rows:
        return {}

    date = cfg.get("meta", {}).get("date") or guess_date_from_files(cfg)
    month = date[:7] if date else ""
    ep = str(cfg.get("meta", {}).get("episode_number") or "").strip()

    def get(row, *keys):
        for k in keys:
            nk = normalize_header(k)
            if row.get(nk):
                return row.get(nk)
        return ""

    def row_date(row):
        return get(row, "date", "datum", "datum konání", "datum konani", "termín", "termin")

    def row_month(row):
        return get(row, "month", "měsíc", "mesic", "období", "obdobi")

    def row_episode(row):
        return get(row, "episode_number", "číslo dílu", "cislo dilu", "díl", "dil", "epizoda", "číslo", "cislo")

    best = None
    # Exact date first
    for r in rows:
        rd = row_date(r)
        if date and rd:
            rd_norm = rd.strip()
            # Accept YYYY-MM-DD and DD.MM.YYYY
            m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})", rd_norm)
            if m:
                rd_norm = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            if rd_norm[:10] == date:
                best = r
                break
    # Episode number
    if not best and ep:
        for r in rows:
            rep = row_episode(r)
            if rep and str(rep).strip().lstrip("#") == ep.lstrip("#"):
                best = r
                break
    # Month fallback
    if not best and month:
        for r in rows:
            rm = row_month(r)
            rd = row_date(r)
            if (rm and month in rm) or (rd and rd[:7] == month):
                best = r
                break
    if not best:
        return {"_debug": f"CSV načteno, ale nenalezen řádek pro datum {date or '?'} / díl {ep or '?'}"}

    return {
        "date": row_date(best) or date,
        "episode_number": row_episode(best),
        "topic": get(best, "topic", "téma", "tema", "název tématu", "nazev tematu", "název", "nazev"),
        "speaker": get(best, "speaker", "řečník", "recnik", "host", "lektor", "přednášející", "prednasejici"),
        "source": "topics.csv: " + Path(used_path).name
    }


def analyze_silences(path):
    if not path or not Path(path).exists():
        return []
    cmd = ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=n=-35dB:d=3", "-f", "null", "-"]
    code, out, err = run(cmd)
    silences = []
    starts = []
    for line in (out + "\n" + err).splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m: starts.append(float(m.group(1)))
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and starts:
            st = starts.pop(0); en = float(m.group(1))
            silences.append({"label": "Ticho > 3 s", "kind": "silence", "start": st, "end": en, "text": f"{seconds_to_time(st)}–{seconds_to_time(en)}"})
    return silences[:80]

def quick_sine_wav(path, seconds=2):
    rate = 44100
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        for i in range(int(rate*seconds)):
            val = int(2000*math.sin(2*math.pi*440*i/rate))
            w.writeframes(struct.pack("<h", val))


def scan_arbochat_folder(folder_path):
    """Scan a local folder on the Mac and map Zoom files without uploading huge video bytes via browser."""
    folder = Path(folder_path).expanduser()
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Složka neexistuje: {folder}")
    files = [p for p in folder.iterdir() if p.is_file()]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
    texts = [p for p in files if p.suffix.lower() in TEXT_EXTS]

    gallery = None
    for p in videos:
        if re.search(r"gallery|galerie", p.name, re.I):
            gallery = p
            break

    speaker = None
    for p in videos:
        if p == gallery:
            continue
        if re.search(r"recording|speaker|present|prezent", p.name, re.I):
            speaker = p
            break
    if not speaker:
        for p in videos:
            if p != gallery:
                speaker = p
                break

    transcript = None
    for p in texts:
        if re.search(r"transcript|přepis|prepis|recording", p.name, re.I):
            transcript = p
            break
    if not transcript and texts:
        transcript = texts[0]

    replacement = None
    for p in videos:
        if p not in (speaker, gallery) and not re.search(r"recording|gallery|galerie|speaker|prezent|present", p.name, re.I):
            replacement = p
            break

    # If naming does not help, use the shortest remaining video as the doprovodné video.
    # It is normally much shorter than the two Zoom recordings.
    if not replacement:
        candidates = [p for p in videos if p not in (speaker, gallery)]
        if candidates:
            def score_video(path):
                try:
                    info = ffprobe(path)
                    dur = float(info.get("duration") or 999999)
                except Exception:
                    dur = 999999
                try:
                    size = path.stat().st_size
                except Exception:
                    size = 999999999999
                return (dur, size)
            replacement = sorted(candidates, key=score_video)[0]

    mapped = {}
    if speaker: mapped["speaker_video"] = str(speaker)
    if gallery: mapped["gallery_video"] = str(gallery)
    if transcript: mapped["transcript_file"] = str(transcript)
    if replacement: mapped["replacement_video"] = str(replacement)
    return mapped


def _truthy_path(value):
    if not value:
        return False
    try:
        return Path(str(value)).expanduser().exists()
    except Exception:
        return False


def _merge_preserving_existing(existing, incoming, *, preserve_paths=False):
    """Merge dictionaries without accidentally erasing already selected files/cuts.

    FULL AUTO saves the live React project just before composing. In older builds
    that save could contain a stale/partial `files` object and overwrite the
    backend project that already knew the gallery and replacement files from
    /api/choose_directory. This helper keeps existing non-empty values unless
    the incoming value is also meaningful. For path dictionaries it additionally
    keeps existing valid paths when incoming values are empty or missing.
    """
    base = dict(existing or {})
    for k, v in (incoming or {}).items():
        if v is None or v == "":
            continue
        if preserve_paths and k in base and _truthy_path(base.get(k)) and not _truthy_path(v):
            continue
        if isinstance(base.get(k), dict) and isinstance(v, dict):
            base[k] = _merge_preserving_existing(base.get(k), v, preserve_paths=preserve_paths)
        else:
            base[k] = v
    return base


def _recover_project_files_from_folder(cfg):
    """Re-scan the original Zoom folder when stale frontend state lost file roles."""
    folder = (cfg.get("source") or {}).get("folder")
    if not folder:
        return cfg
    try:
        mapped = scan_arbochat_folder(folder)
    except Exception:
        return cfg
    files = cfg.setdefault("files", {})
    changed = False
    for role, value in mapped.items():
        if value and (not files.get(role) or not _truthy_path(files.get(role))):
            files[role] = value
            changed = True
    if changed:
        try:
            print("Obnoveny role ze slozky:", ", ".join(sorted(mapped.keys())), flush=True)
        except Exception:
            pass
    return cfg


def _media_duration(src):
    if not src or not Path(str(src)).exists():
        return 0.0
    try:
        return float(ffprobe(src).get("duration") or 0)
    except Exception:
        return 0.0


# --- ArboChat audio character detection patch ---
def _median(values):
    values = [v for v in values if v is not None]
    if not values:
        return 0.0
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _audio_feature_distance(a_rms, a_zcr, b_rms, b_zcr):
    # log RMS catches loudness/texture change, ZCR catches speech/music/noise character.
    return abs(a_rms - b_rms) * 1.15 + abs(a_zcr - b_zcr) * 12.0


def detect_inserted_video_by_audio(video_path, work_dir, max_seconds=1500):
    """
    Finds a likely inserted video by audio character change.
    Standard-library only: FFmpeg extracts a low-rate mono WAV, Python measures
    1-second RMS and zero-crossing rate.
    """
    if not video_path or not Path(video_path).exists():
        return []

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    wav_path = work_dir / "audio_character_8k_mono.wav"

    # Cache the feature WAV. Delete it if you need to force recalculation.
    if not wav_path.exists():
        cmd = [
            "ffmpeg", "-y", "-hide_banner",
            "-t", str(max_seconds),
            "-i", str(video_path),
            "-vn",
            "-ac", "1",
            "-ar", "8000",
            "-acodec", "pcm_s16le",
            str(wav_path)
        ]
        code, so, se = run(cmd)
        if code != 0 or not wav_path.exists():
            return [{
                "label": "Audio detekce vloženého videa selhala",
                "kind": "warning",
                "start": 0,
                "end": 0,
                "text": (se or so or "FFmpeg nevytvořil pracovní audio")[-300:]
            }]

    import wave, struct, math

    features = []

    try:
        with wave.open(str(wav_path), "rb") as w:
            rate = w.getframerate()
            width = w.getsampwidth()
            channels = w.getnchannels()
            if width != 2:
                return []

            frame_count = rate  # 1 second windows

            t = 0
            while True:
                data = w.readframes(frame_count)
                if not data:
                    break

                count = len(data) // 2
                if count <= 10:
                    break

                samples = struct.unpack("<" + "h" * count, data)

                # RMS
                ss = 0.0
                crossings = 0
                prev = samples[0]

                for x in samples:
                    ss += x * x
                    if (x >= 0 and prev < 0) or (x < 0 and prev >= 0):
                        crossings += 1
                    prev = x

                rms = math.sqrt(ss / count)
                log_rms = math.log(max(rms, 1.0))
                zcr = crossings / max(1, count - 1)

                features.append({
                    "t": t,
                    "rms": rms,
                    "log_rms": log_rms,
                    "zcr": zcr,
                })
                t += 1

    except Exception as e:
        return [{
            "label": "Audio analýza selhala",
            "kind": "warning",
            "start": 0,
            "end": 0,
            "text": str(e)
        }]

    n = len(features)
    if n < 180:
        return []

    logs = [f["log_rms"] for f in features]
    zcrs = [f["zcr"] for f in features]
    rmss = [f["rms"] for f in features]

    # Search likely inserted-video zone: after intro, before lecture settles.
    # This is deliberately wide; current known case is around 00:07:53.
    search_start = min(max(90, int(n * 0.03)), n - 120)
    search_end = min(n - 45, 20 * 60)

    best_t = None
    best_score = -1.0

    for t in range(search_start, search_end):
        prev_a = max(0, t - 70)
        prev_b = max(0, t - 10)
        next_a = min(n, t + 5)
        next_b = min(n, t + 45)

        if prev_b <= prev_a or next_b <= next_a:
            continue

        prev_r = _median(logs[prev_a:prev_b])
        prev_z = _median(zcrs[prev_a:prev_b])
        next_r = _median(logs[next_a:next_b])
        next_z = _median(zcrs[next_a:next_b])
        next_rms = _median(rmss[next_a:next_b])

        # Ignore pure silence transitions.
        if next_rms < 80:
            continue

        score = _audio_feature_distance(prev_r, prev_z, next_r, next_z)

        # Mild prior: inserted video often appears after moderator intro.
        if 5 * 60 <= t <= 12 * 60:
            score *= 1.12

        if score > best_score:
            best_score = score
            best_t = t

    if best_t is None:
        return []

    # Estimate end: look for a return towards pre-video character,
    # or a second major transition after the detected start.
    base_r = _median(logs[max(0, best_t - 70):max(0, best_t - 10)])
    base_z = _median(zcrs[max(0, best_t - 70):max(0, best_t - 10)])
    insert_r = _median(logs[min(n, best_t + 10):min(n, best_t + 70)])
    insert_z = _median(zcrs[min(n, best_t + 10):min(n, best_t + 70)])

    end_t = None
    return_streak = 0

    for t in range(best_t + 45, min(n - 10, best_t + 8 * 60)):
        cur_r = _median(logs[t:min(n, t + 12)])
        cur_z = _median(zcrs[t:min(n, t + 12)])

        dist_base = _audio_feature_distance(cur_r, cur_z, base_r, base_z)
        dist_insert = _audio_feature_distance(cur_r, cur_z, insert_r, insert_z)

        if dist_base + 0.08 < dist_insert:
            return_streak += 1
            if return_streak >= 8:
                end_t = t - 7
                break
        else:
            return_streak = 0

    # If return detection fails, find strongest later transition.
    if end_t is None:
        best_end = None
        best_end_score = -1.0

        for t in range(best_t + 50, min(n - 45, best_t + 8 * 60)):
            prev_r = _median(logs[max(0, t - 40):max(0, t - 5)])
            prev_z = _median(zcrs[max(0, t - 40):max(0, t - 5)])
            next_r = _median(logs[min(n, t + 5):min(n, t + 40)])
            next_z = _median(zcrs[min(n, t + 5):min(n, t + 40)])

            score = _audio_feature_distance(prev_r, prev_z, next_r, next_z)

            if score > best_end_score:
                best_end_score = score
                best_end = t

        if best_end is not None and best_end_score > 0.35:
            end_t = best_end

    markers = [{
        "label": "Audio: změna charakteru — kandidát začátku vloženého videa",
        "kind": "replacement",
        "start": float(best_t),
        "end": float(best_t + 8),
        "text": f"Detekována výrazná změna zvuku. Skóre {best_score:.2f}. Ověř poslechem a případně nastav jako začátek vloženého videa."
    }]

    if end_t is not None and end_t > best_t:
        markers.append({
            "label": "Audio: návrat / další změna — kandidát konce vloženého videa",
            "kind": "replacement_end",
            "start": float(end_t),
            "end": float(end_t + 8),
            "text": "Odhad konce vloženého videa podle návratu nebo další změny charakteru zvuku."
        })

    return markers


def detect_optimized_start_from_transcript(entries):
    """
    Prefer real ArboChat opening, not earlier technical testing.
    """
    if not entries:
        return []

    strong_patterns = [
        r"dobrý den.*vítám.*arbochat",
        r"dobry den.*vitam.*arbochat",
        r"vítám vás.*arbochat",
        r"vitam vas.*arbochat",
        r"u další(?:ho)? arbochatu",
        r"u dalsi(?:ho)? arbochatu",
        r"dnešní(?:m)? tématem je",
        r"dnes(?:ka)? si budeme povídat",
        r"naším hostem je",
        r"nasim hostem je",
    ]

    candidates = []

    for e in entries:
        txt = (e.get("text") or "").lower()
        for pat in strong_patterns:
            if re.search(pat, txt, flags=re.I):
                candidates.append({
                    "label": "Optimalizovaný kandidát reálného začátku",
                    "kind": "start",
                    "start": e.get("start", 0),
                    "end": e.get("end", e.get("start", 0) + 5),
                    "text": e.get("text", "")[:240]
                })
                break

    # Prefer a candidate after initial technical chatter, but don't skip a very early real intro.
    if candidates:
        after_30 = [c for c in candidates if c["start"] >= 30]
        return [after_30[0] if after_30 else candidates[0]]

    return []
# --- end ArboChat audio character detection patch ---



# ---------------- React rewrite backend ----------------
def _json_error(message, status=400):
    return {"ok": False, "error": message, "status": status}


def _choose_macos(kind="folder", prompt="Vyber soubor"):
    if kind == "folder":
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
    else:
        script = f'POSIX path of (choose file with prompt "{prompt}")'
    p = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "Výběr zrušen")[-500:])
    return p.stdout.strip()


def _content_type(path):
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _source_for_project(cfg, role=None):
    files = cfg.get("files", {})
    if role and files.get(role):
        return files.get(role)
    return files.get("speaker_video") or files.get("gallery_video") or files.get("replacement_video")


def _make_preview_frame(pid, t, role=None):
    cfg = project_config(pid)
    src = _source_for_project(cfg, role)
    if not src or not Path(src).exists():
        raise FileNotFoundError("Zdrojové video pro náhled neexistuje")
    out = project_dir(pid) / "work" / f"frame_{role or 'main'}_{int(float(t)*10)}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", _ffmpeg_time(t), "-i", str(src), "-frames:v", "1", "-vf", "scale=360:-2", str(out)]
        code, so, se = run(cmd)
        if code != 0 or not out.exists():
            raise RuntimeError((se or so or "FFmpeg nevytvořil obrázek z videa")[-900:])
    return out


def _make_video_preview(pid, t, seconds=5, role=None):
    cfg = project_config(pid)
    src = _source_for_project(cfg, role)
    if not src or not Path(src).exists():
        raise FileNotFoundError("Zdrojové video pro náhled neexistuje")
    t = float(t); seconds = float(seconds)
    start = max(0, t - seconds)
    out = project_dir(pid) / "work" / f"video_preview_{role or 'main'}_{int(start*10)}_{int(t*10)}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-ss", str(start), "-i", str(src), "-t", str(max(1, seconds)),
            "-vf", "scale=960:-2,format=yuv420p", *video_encode_args("24", vt_bitrate="3M"),
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)
        ]
        code, so, se = run(cmd)
        if code != 0 or not out.exists():
            raise RuntimeError((se or so or "FFmpeg nevytvořil video náhled")[-900:])
    return out


def _make_audio_preview(pid, start, end):
    cfg = project_config(pid)
    src = _source_for_project(cfg)
    if not src or not Path(src).exists():
        raise FileNotFoundError("Zdrojové video pro audio náhled neexistuje")
    start = float(start); end = float(end); duration = max(1.0, end - start)
    out = project_dir(pid) / "work" / f"audio_preview_{int(start*10)}_{int(end*10)}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", str(max(0, start)), "-i", str(src), "-t", str(duration), "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", str(out)]
        code, so, se = run(cmd)
        if code != 0 or not out.exists():
            raise RuntimeError((se or so or "FFmpeg nevytvořil audio náhled")[-900:])
    return out


def _waveform_cache_wav(pid, src, role=None):
    work = project_dir(pid) / "work"
    work.mkdir(parents=True, exist_ok=True)
    safe_role = re.sub(r"[^a-zA-Z0-9_-]+", "_", role or "main")
    wav = work / f"waveform_{safe_role}_8k_mono.wav"
    marker = work / f"waveform_{safe_role}_source.txt"
    src_s = str(src)
    if not wav.exists() or not marker.exists() or marker.read_text(encoding="utf-8", errors="ignore") != src_s:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000", "-acodec", "pcm_s16le", str(wav)]
        code, so, se = run(cmd)
        if code != 0 or not wav.exists():
            raise RuntimeError((se or so or "FFmpeg nevytvořil waveform audio")[-900:])
        marker.write_text(src_s, encoding="utf-8")
    return wav


def _waveform_peaks(pid, width=1800, role=None):
    cfg = project_config(pid)
    src = _source_for_project(cfg, role)
    if not src or not Path(src).exists():
        raise FileNotFoundError("Zdrojové video pro waveform neexistuje")
    wav = _waveform_cache_wav(pid, src, role or "speaker_video")
    import wave, struct, math
    width = int(max(300, min(120000, width)))

    with wave.open(str(wav), "rb") as w:
        n = w.getnframes()
        rate = w.getframerate() or 8000
        if n <= 0:
            return {"duration": 0, "peaks": []}

        bucket = max(1, math.ceil(n / width))
        peaks = []
        mn = 32767
        mx = -32768
        seen = 0
        total_seen = 0
        read_size = max(4096, bucket * 4)

        while True:
            raw = w.readframes(read_size)
            if not raw:
                break
            count = len(raw) // 2
            if count <= 0:
                break
            samples = struct.unpack("<" + "h" * count, raw)
            for sample in samples:
                if sample < mn:
                    mn = sample
                if sample > mx:
                    mx = sample
                seen += 1
                total_seen += 1
                if seen >= bucket:
                    peaks.append([round(mn / 32768.0, 5), round(mx / 32768.0, 5)])
                    mn = 32767
                    mx = -32768
                    seen = 0

        if seen > 0:
            peaks.append([round(mn / 32768.0, 5), round(mx / 32768.0, 5)])

    return {"duration": n / float(rate), "peaks": peaks}


def detect_lecture_start_by_audio(video_path, work_dir, max_seconds=1800):
    """
    Finds a likely beginning of the actual lecture/program from audio.
    It looks for the first sustained, speech-like energy after the early technical part.
    """
    if not video_path or not Path(video_path).exists():
        return []
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    wav_path = work_dir / "lecture_start_8k_mono.wav"
    marker = work_dir / "lecture_start_source.txt"
    src_s = str(video_path)
    if not wav_path.exists() or not marker.exists() or marker.read_text(encoding="utf-8", errors="ignore") != src_s:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-t", str(max_seconds),
            "-i", str(video_path), "-vn", "-ac", "1", "-ar", "8000",
            "-acodec", "pcm_s16le", str(wav_path)
        ]
        code, so, se = run(cmd)
        if code != 0 or not wav_path.exists():
            return [{"label":"Audio start: analýza selhala", "kind":"warning", "start":0, "end":0, "text":(se or so or "FFmpeg nevytvořil pracovní audio")[-300:]}]
        marker.write_text(src_s, encoding="utf-8")

    import wave, struct, math
    features = []
    try:
        with wave.open(str(wav_path), "rb") as w:
            rate = w.getframerate()
            frame_count = rate
            t = 0
            while True:
                data = w.readframes(frame_count)
                if not data:
                    break
                count = len(data) // 2
                if count <= 10:
                    break
                samples = struct.unpack("<" + "h" * count, data)
                ss = 0.0
                crossings = 0
                prev = samples[0]
                for x in samples:
                    ss += x * x
                    if (x >= 0 and prev < 0) or (x < 0 and prev >= 0):
                        crossings += 1
                    prev = x
                rms = math.sqrt(ss / count)
                zcr = crossings / max(1, count - 1)
                features.append({"t":t, "rms":rms, "log_rms":math.log(max(rms, 1.0)), "zcr":zcr})
                t += 1
    except Exception as e:
        return [{"label":"Audio start: analýza selhala", "kind":"warning", "start":0, "end":0, "text":str(e)}]

    if len(features) < 40:
        return []

    early = [f["rms"] for f in features[:min(60, len(features))]]
    baseline = _median(early) or 1.0
    all_rms = sorted(f["rms"] for f in features)
    p70 = all_rms[int(len(all_rms) * 0.70)] if all_rms else baseline
    threshold = max(baseline * 2.2, p70 * 0.55, 120.0)

    best_t = None
    best_score = -1.0
    # Do not start immediately at 0 unless there is no better candidate.
    first_search = 10
    last_search = min(len(features) - 12, max_seconds)
    for t in range(first_search, last_search):
        window = features[t:t+12]
        if len(window) < 8:
            continue
        active = sum(1 for f in window if f["rms"] >= threshold)
        median_rms = _median([f["rms"] for f in window])
        median_zcr = _median([f["zcr"] for f in window])
        # Speech-like audio is usually not pure music/noise nor dead silence.
        if active < 7 or median_zcr < 0.015 or median_zcr > 0.22:
            continue
        prev = features[max(0, t-25):t]
        prev_med = _median([f["rms"] for f in prev]) if prev else baseline
        score = (median_rms / max(1.0, prev_med)) + (median_rms / max(1.0, threshold))
        # Prefer starts after early Zoom/testing chatter, but still allow early starts.
        if 60 <= t <= 12 * 60:
            score *= 1.25
        if score > best_score:
            best_score = score
            best_t = t
            # The first strong sustained candidate is usually the useful one.
            if t > 25 and score > 3.0:
                break

    if best_t is None:
        return []

    return [{
        "label": "Audio: pravděpodobné zahájení přednášky",
        "kind": "lecture_start_audio",
        "start": float(best_t),
        "end": float(best_t + 8),
        "text": f"Odhad podle nástupu souvislé zvukové aktivity. Skóre {best_score:.2f}. Ověř poslechem."
    }]


def _seconds(value, default=0):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    parsed = parse_time_to_seconds(value)
    return default if parsed is None else float(parsed)


def _ffmpeg_time(value):
    """Format seconds for ffmpeg; avoid scientific notation such as 5e-05."""
    try:
        x = float(value)
    except Exception:
        x = 0.0
    if abs(x) < 0.001:
        x = 0.0
    if x < 0:
        x = 0.0
    return f"{x:.6f}"


def _analyze_project(pid):
    cfg = project_config(pid)
    files = cfg.get("files", {})
    entries, plain = parse_transcript(files.get("transcript_file"))
    media = {}
    for role, path in files.items():
        if path and Path(path).exists() and Path(path).suffix.lower() in VIDEO_EXTS:
            media[role] = ffprobe(path)

    markers = []
    markers.extend(detect_optimized_start_from_transcript(entries))

    # These three audio scans each do their own full ffmpeg decode pass over the
    # same speaker video and don't depend on each other's output, so they used to
    # run back-to-back on the request thread. Running them concurrently (they are
    # subprocess/IO bound, so the GIL isn't a bottleneck) cuts wall-clock time for
    # this step roughly to the slowest single scan instead of the sum of all three.
    # Marker order doesn't matter: everything is sorted by start/end time below.
    speaker_video = files.get("speaker_video")
    if speaker_video:
        work_dir = project_dir(pid) / "work"
        audio_tasks = {
            "lecture_start": lambda: detect_lecture_start_by_audio(speaker_video, work_dir),
            "silences": lambda: analyze_silences(speaker_video),
            "inserted": lambda: detect_inserted_video_by_audio(speaker_video, work_dir),
        }
        with ThreadPoolExecutor(max_workers=len(audio_tasks)) as pool:
            futures = {pool.submit(fn): name for name, fn in audio_tasks.items()}
            audio_results = {}
            for fut in futures:
                name = futures[fut]
                try:
                    audio_results[name] = fut.result() or []
                except Exception as e:
                    audio_results[name] = [{"label": "Audio start: chyba", "kind": "warning", "start": 0, "end": 0, "text": str(e)}]
        markers.extend(audio_results.get("lecture_start") or [])
        markers.extend(audio_results.get("silences") or [])
        markers.extend(audio_results.get("inserted") or [])

    markers.extend(find_phrase_entries(entries, [r"vid[ií]te.*prezentaci", r"je vid[eě]t.*prezentace", r"sd[ií]len[íi] obrazovky"], "Kontrola viditelnosti prezentace", "presentation_check"))
    markers.extend(find_phrase_entries(entries, [r"dotazy", r"diskuse", r"ot[aá]zky", r"m[uů][zž]ete se pt[aá]t"], "Kandidát začátku diskuse", "discussion"))
    markers.extend(detect_terms(entries))

    # Safe fallbacks from current known workflow.
    if not any(m.get("kind") == "replacement" for m in markers):
        markers.append({"label": "Fallback začátek vloženého videa", "kind": "replacement", "start": 7*60+53, "end": 7*60+58, "text": "Fallback podle typického místa v aktuálních nahrávkách."})
    if not any(m.get("kind") == "discussion" for m in markers):
        markers.append({"label": "Fallback začátek diskuse", "kind": "discussion", "start": 1*3600+19*60+52, "end": 1*3600+19*60+57, "text": "Fallback podle typického místa v aktuálních nahrávkách."})

    meta = cfg.get("meta", {}) or {}
    transcript_meta = extract_metadata_from_transcript(entries, plain)
    topic_meta = match_topics(cfg, transcript_meta)
    if guess_date_from_files(cfg) and not meta.get("date"):
        meta["date"] = guess_date_from_files(cfg)
    for key in ["date", "episode_number", "topic", "speaker"]:
        if not meta.get(key):
            meta[key] = topic_meta.get(key) or transcript_meta.get(key, "")
    meta["metadata_source"] = topic_meta.get("source") or ("transkript" if transcript_meta else "")

    markers = sorted(markers, key=lambda m: (float(m.get("start", 0) or 0), float(m.get("end", 0) or 0)))[:500]
    cfg["meta"] = meta
    cfg["analysis"] = {"media": media, "markers": markers, "transcript_entries": len(entries), "analyzed_at": datetime.datetime.now().isoformat(timespec="seconds")}

    cuts = cfg.setdefault("cuts", {})
    if "real_start" not in cuts or cuts.get("real_start") in (None, ""):
        start_mark = next((m for m in markers if m.get("kind") == "start"), None)
        cuts["real_start"] = float(start_mark.get("start", 0)) if start_mark else 0.0
    if "replacement_start" not in cuts or cuts.get("replacement_start") in (None, ""):
        repl = next((m for m in markers if m.get("kind") == "replacement"), None)
        if repl: cuts["replacement_start"] = float(repl.get("start", 0))
    if "replacement_end" not in cuts or cuts.get("replacement_end") in (None, ""):
        repl_end = next((m for m in markers if m.get("kind") == "replacement_end"), None)
        if repl_end: cuts["replacement_end"] = float(repl_end.get("start", 0))
    if "discussion_start" not in cuts or cuts.get("discussion_start") in (None, ""):
        disc = next((m for m in markers if m.get("kind") == "discussion"), None)
        if disc: cuts["discussion_start"] = float(disc.get("start", 0))

    save_project(cfg)
    return cfg


def _finalize_edit(pid):
    cfg = project_config(pid)
    files = cfg.get("files", {})
    cuts = cfg.get("cuts", {})
    speaker = files.get("speaker_video")
    if not speaker or not Path(speaker).exists():
        raise RuntimeError("Chybí hlavní video pro finální úpravu")

    media = cfg.get("analysis", {}).get("media", {}) or {}
    if "speaker_video" not in media:
        media["speaker_video"] = ffprobe(speaker)
    total = float(media.get("speaker_video", {}).get("duration") or ffprobe(speaker).get("duration") or 0)

    real = _seconds(cuts.get("real_start"), 0)
    main_end = _seconds(cuts.get("main_end"), total)
    if main_end <= real:
        main_end = total
    replacement_start = _seconds(cuts.get("replacement_start"), -1)
    replacement_end = _seconds(cuts.get("replacement_end"), -1)
    discussion_start = _seconds(cuts.get("discussion_start"), -1)

    # Finalni analyza tich se spousti az po rucnim strihu.
    raw_silences = analyze_silences(speaker)
    final_silences = []
    for m in raw_silences:
        st = float(m.get("start", 0) or 0)
        en = float(m.get("end", st) or st)
        if en < real or st > main_end:
            continue
        final_silences.append({
            **m,
            "start": max(st, real),
            "end": min(en, main_end),
            "text": f"Finální ticho po trimu: {seconds_to_time(max(st, real))}–{seconds_to_time(min(en, main_end))}"
        })

    settings = load_settings()
    intro = settings.get("intro_template")
    outro = settings.get("outro_template")
    meta = cfg.get("meta", {}) or {}
    desc_parts = []
    if meta.get("topic"):
        desc_parts.append(str(meta.get("topic")))
    if meta.get("speaker"):
        desc_parts.append("řečník: " + str(meta.get("speaker")))
    if meta.get("date"):
        desc_parts.append("datum: " + str(meta.get("date")))
    description = " · ".join(desc_parts)

    analysis = cfg.setdefault("analysis", {})
    markers = [m for m in analysis.get("markers", []) if m.get("kind") != "final_silence"]
    markers.extend([{**m, "kind": "final_silence", "label": "Finální ticho po úpravě"} for m in final_silences])
    analysis["markers"] = sorted(markers, key=lambda m: (float(m.get("start", 0) or 0), float(m.get("end", 0) or 0)))[:700]
    analysis["final_edit"] = {
        "prepared_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "real_start": real,
        "main_end": main_end,
        "replacement_start": replacement_start,
        "replacement_end": replacement_end,
        "discussion_start": discussion_start,
        "silence_count": len(final_silences),
        "silences": final_silences[:120],
        "intro": bool(intro and Path(intro).exists()),
        "outro": bool(outro and Path(outro).exists()),
        "description": description,
        "ready_for_render": True,
    }
    cfg["analysis"] = analysis
    save_project(cfg)
    return cfg



def _segment_duration(src, start=0.0, duration=None, fallback=5.0):
    """Return a safe segment duration for ffmpeg helpers."""
    if duration is not None:
        try:
            return max(0.0, float(duration))
        except Exception:
            return 0.0
    try:
        total = float(ffprobe(src).get("duration") or 0)
        if total > float(start or 0) + 0.1:
            return max(0.0, total - float(start or 0))
    except Exception:
        pass
    return float(fallback or 5.0)


def _normalized_segment(src, start, duration, target):
    """Create one reliable Full HD segment with a video stream and an audio stream.

    Older builds let FFmpeg choose streams automatically. That made concatenation
    fragile and could silently drop replacement/gallery segments when one source
    had an unusual audio layout. This helper now always maps video explicitly and
    adds silent stereo audio when the source has no usable audio track.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    seg_dur = _segment_duration(src, start, duration)
    if seg_dur <= 0.25:
        return None
    dur_args = ["-t", _ffmpeg_time(seg_dur)]
    base = ["ffmpeg", "-y", "-hide_banner", "-ss", _ffmpeg_time(start), "-i", str(src), *dur_args]
    vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    if _media_has_audio(src):
        cmd = base + [
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", vf, "-r", "30", *video_encode_args("20"),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(target)
        ]
    else:
        cmd = base + [
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={_ffmpeg_time(seg_dur)}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", vf, "-r", "30", *video_encode_args("20"),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(target)
        ]
    code, so, se = run(cmd)
    if code != 0 or not target.exists():
        raise RuntimeError((se or so or f"Nelze vytvořit segment {target.name}")[-1500:])
    return target



def _overlay_timeline_segment(base_src, overlay_src, base_start, overlay_start, duration, target, prefer_overlay_audio=True):
    """Create a Full HD segment by putting overlay_src visually over base_src.

    This is intentionally used for the replacement clip and the gallery part of
    the timeline. A simple concatenated gallery segment can look as if the main
    video continued when Zoom exports have matching audio/timecodes or when a
    later step rewrites streams. This helper makes the visual priority explicit:
    the top video is scaled to the full 1920x1080 canvas and placed over the
    main video for the whole segment duration.
    """
    if not base_src or not overlay_src or not Path(base_src).exists() or not Path(overlay_src).exists():
        return None
    try:
        base_start = max(0.0, float(base_start or 0.0))
        overlay_start = max(0.0, float(overlay_start or 0.0))
        duration = float(duration or 0.0)
    except Exception:
        return None
    if duration <= 0.25:
        return None
    try:
        btot = float(ffprobe(base_src).get("duration") or 0)
        if btot > 0:
            duration = min(duration, max(0.0, btot - base_start))
    except Exception:
        pass
    try:
        otot = float(ffprobe(overlay_src).get("duration") or 0)
        if otot > 0:
            duration = min(duration, max(0.0, otot - overlay_start))
    except Exception:
        pass
    if duration <= 0.25:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[base];"
        "[1:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[top];"
        "[base][top]overlay=0:0:shortest=1,format=yuv420p[v]"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-ss", _ffmpeg_time(base_start), "-i", str(base_src),
        "-ss", _ffmpeg_time(overlay_start), "-i", str(overlay_src),
        "-t", _ffmpeg_time(duration),
    ]
    overlay_audio = _media_has_audio(overlay_src)
    base_audio = _media_has_audio(base_src)
    if prefer_overlay_audio and overlay_audio:
        audio_map = ["-map", "1:a:0"]
    elif base_audio:
        audio_map = ["-map", "0:a:0"]
    elif overlay_audio:
        audio_map = ["-map", "1:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={_ffmpeg_time(duration)}"]
        audio_map = ["-map", "2:a:0"]
    cmd += [
        "-filter_complex", vf,
        "-map", "[v]", *audio_map,
        "-r", "30", *video_encode_args("20"),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", str(target)
    ]
    code, so, se = run(cmd)
    if code != 0 or not target.exists():
        raise RuntimeError((se or so or f"Nelze vytvořit překryvný segment {target.name}")[-1500:])
    return target

def _edit_master_source(cfg):
    files = cfg.get("files", {})
    src = files.get("edit_master_video") or files.get("edit_master_original")
    if not src or not Path(src).exists():
        raise RuntimeError("Pracovní video ještě neexistuje. Nejdřív stiskni Upravit video.")
    return src

def _intro_screen_items(cfg):
    meta = cfg.get("meta", {}) or {}
    layout = meta.get("intro_layout") or {}
    defs = [
        ("episode_number", "Číslo dílu", f"ArboChat #{meta.get('episode_number')}", 130, 170, 46, 800),
        ("topic", "Téma", str(meta.get("topic") or ""), 130, 250, 54, 800),
        ("speaker", "Řečník", ("Řečník: " + str(meta.get("speaker"))) if meta.get("speaker") else "", 130, 340, 34, 650),
        ("date", "Datum", ("Datum: " + str(meta.get("date"))) if meta.get("date") else "", 130, 400, 30, 650),
    ]
    items = []
    for key, label, text, x, y, size, weight in defs:
        custom = layout.get(key) if isinstance(layout, dict) else {}
        if not text:
            continue
        items.append({
            "key": key,
            "label": label,
            "text": text,
            "x": int(float(custom.get("x", x) if isinstance(custom, dict) else x)),
            "y": int(float(custom.get("y", y) if isinstance(custom, dict) else y)),
            "size": int(float(custom.get("size", size) if isinstance(custom, dict) else size)),
            "font": str(custom.get("font", "Inter") if isinstance(custom, dict) else "Inter"),
            "weight": int(weight),
        })
    if not items:
        items = [{"key":"project", "label":"Projekt", "text":cfg.get("name") or "ArboChat", "x":130, "y":300, "size":52, "weight":800}]
    return items


def _intro_screen_lines(cfg):
    return [x.get("text", "") for x in _intro_screen_items(cfg)]


def _intro_template_frame(cfg, target):
    settings = load_settings()
    intro = settings.get("intro_template")
    if not intro or not Path(intro).exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        dur = float(ffprobe(intro).get("duration") or 0)
    except Exception:
        dur = 5.0
    ss = max(0.0, min(dur - 0.05 if dur > 0.1 else 0.0, 5.0 if dur >= 5.0 else dur * 0.65))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-ss", f"{ss:.3f}", "-i", str(intro), "-frames:v", "1",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-q:v", "3", str(target)
    ]
    code, so, se = run(cmd)
    if code == 0 and target.exists():
        return target
    return None


def _intro_background_frame(pid):
    cfg = project_config(pid)
    work = project_dir(pid) / "work"
    frame = _intro_template_frame(cfg, work / "intro_screen_bg.jpg")
    if frame and Path(frame).exists():
        return frame
    fallback = work / "intro_screen_bg.svg"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720"><defs><linearGradient id="bg" x1="0" x2="1" y1="0" y2="1"><stop offset="0" stop-color="#0f172a"/><stop offset="1" stop-color="#14532d"/></linearGradient></defs><rect width="1280" height="720" fill="url(#bg)"/><circle cx="1080" cy="120" r="180" fill="#22c55e" opacity="0.16"/><circle cx="180" cy="620" r="230" fill="#38bdf8" opacity="0.12"/></svg>', encoding="utf-8")
    return fallback



def _safe_intro_font_name(name):
    allowed = {
        "Inter": "Inter",
        "Arial": "Arial",
        "Helvetica": "Helvetica",
        "Georgia": "Georgia",
        "Times New Roman": "Times New Roman",
        "Courier New": "Courier New",
        "Verdana": "Verdana",
    }
    return allowed.get(str(name or "Inter"), "Inter")


def _fontfile_for_intro(name):
    font = _safe_intro_font_name(name)
    candidates = {
        "Inter": [
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ],
        "Arial": ["/System/Library/Fonts/Supplemental/Arial.ttf"],
        "Helvetica": ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"],
        "Georgia": ["/System/Library/Fonts/Supplemental/Georgia.ttf"],
        "Times New Roman": ["/System/Library/Fonts/Supplemental/Times New Roman.ttf"],
        "Courier New": ["/System/Library/Fonts/Supplemental/Courier New.ttf"],
        "Verdana": ["/System/Library/Fonts/Supplemental/Verdana.ttf"],
    }.get(font, [])
    for c in candidates:
        if Path(c).exists():
            return c
    fallback = "/System/Library/Fonts/Supplemental/Arial.ttf"
    return fallback if Path(fallback).exists() else ""



def _ffmpeg_filter_value_escape(value):
    return str(value).replace('\\', '\\\\').replace(':', r'\:').replace(',', r'\,').replace("'", r"\'")

def _ffmpeg_expr_escape(expr):
    return expr.replace("\\", "\\\\").replace(",", r"\,").replace(":", r"\:")

def _ffmpeg_filter_path(path):
    # Escape a filesystem path for use inside an FFmpeg filter argument.
    return str(path).replace('\\', '\\\\').replace(':', r'\:').replace("'", r"\'")


def _ass_escape(text):
    return str(text or '').replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}').replace('\n', r'\N')


def _ass_color_from_hex(hex_color, alpha=0):
    # ASS color format is &HAABBGGRR
    h = str(hex_color or '#ffffff').strip().lstrip('#')
    if len(h) != 6:
        h = 'ffffff'
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    aa = f'{max(0, min(255, int(alpha))):02X}'
    return f'&H{aa}{bb}{gg}{rr}'


def _intro_ass_file(cfg, target, duration=5.0):
    """Create ASS subtitles matching the browser preview layout.

    The UI stores positions in a 1280x720 logical canvas. The intro and final
    video are 1920x1080, so the same 1.5 scale used by the SVG preview is used
    here too. This avoids the previous mismatch where the preview and render
    had different coordinates/sizes.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.5, float(duration or 5.0))
    scale = 1.5
    styles = []
    events = []
    items = _intro_screen_items(cfg)[:8]
    for i, it in enumerate(items):
        font = _safe_intro_font_name(it.get('font', 'Inter'))
        size = max(8, int(float(it.get('size', 36)) * scale))
        weight = max(100, min(900, int(it.get('weight', 700))))
        style = f'S{i}'
        # Alignment 7 = top-left anchor, so pos(x,y) matches the browser preview.
        styles.append(f'Style: {style},{font},{size},&H00F8FAFC,&H00000000,&H00000000,&H00000000,{-1 if weight >= 700 else 0},0,0,0,100,100,0,0,1,5,0,7,0,0,0,1')
        x = int(float(it.get('x', 120)) * scale)
        y = int(float(it.get('y', 240)) * scale)
        # Staggered fade out, but keep all text visible long enough.
        fade_ms = 950
        end = max(0.7, duration - max(0, len(items)-1-i) * 0.25)
        h = int(end // 3600); m = int((end % 3600) // 60); sec = end % 60
        end_ts = f'{h}:{m:02d}:{sec:05.2f}'
        text = _ass_escape(it.get('text', ''))
        events.append(f'Dialogue: 0,0:00:00.00,{end_ts},{style},,0,0,0,,{{\\pos({x},{y})\\fad(0,{fade_ms})}}{text}')
    ass = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
""" + "\n".join(styles) + """

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
""" + "\n".join(events) + "\n"
    target.write_text(ass, encoding='utf-8')
    return target



def _intro_overlay_png_backend(cfg, target):
    """Create a transparent 1920x1080 PNG overlay from current intro layout.

    This avoids browser/QuickLook/drawtext/libass dependencies. It uses Pillow when
    available and the same 1280x720 logical coordinate system as the UI preview.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    img = Image.new('RGBA', (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = 1.5

    def font_for(item):
        size = max(8, int(float(item.get('size', 36)) * scale))
        fontfile = _fontfile_for_intro(item.get('font', 'Inter'))
        try:
            if fontfile and Path(fontfile).exists():
                return ImageFont.truetype(fontfile, size=size)
        except Exception:
            pass
        # Linux/container fallback; harmless on macOS if unavailable.
        for cand in [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]:
            try:
                if Path(cand).exists():
                    return ImageFont.truetype(cand, size=size)
            except Exception:
                pass
        return ImageFont.load_default()

    for item in _intro_screen_items(cfg)[:8]:
        text = str(item.get('text') or '')
        if not text.strip():
            continue
        x = int(float(item.get('x', 120)) * scale)
        y = int(float(item.get('y', 240)) * scale)
        font = font_for(item)
        stroke = max(3, int(float(item.get('size', 36)) * scale * 0.09))
        # Browser/SVG uses y as baseline. Pillow uses anchor='ls' for left/baseline.
        try:
            draw.text((x, y), text, font=font, anchor='ls', fill=(248,250,252,255),
                      stroke_width=stroke, stroke_fill=(2,6,23,235))
        except TypeError:
            draw.text((x, y), text, font=font, fill=(248,250,252,255),
                      stroke_width=stroke, stroke_fill=(2,6,23,235))
    img.save(target)
    return target if target.exists() else None

def _browser_intro_overlay_path(pid):
    return project_dir(pid) / "work" / "intro_overlay_browser.png"


def _save_intro_overlay_png(pid, data_url):
    if not pid:
        raise RuntimeError("Chybí projekt")
    raw = str(data_url or "")
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw)
    except Exception as e:
        raise RuntimeError("Nepodařilo se přečíst PNG overlay vstupní obrazovky") from e
    out = _browser_intro_overlay_path(pid)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def _overlay_png_on_video(src, overlay_png, target, duration=None, crf="22"):
    src = Path(src); overlay_png = Path(overlay_png); target = Path(target)
    if not src.exists() or not overlay_png.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    duration = float(duration or ffprobe(src).get("duration") or 0)
    if duration <= 0:
        duration = None
    base = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-loop", "1", "-i", str(overlay_png)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-filter_complex",
        f"[0:v]{base}[bg];[1:v]scale=1920:1080,format=rgba[ov];[bg][ov]overlay=0:0:format=auto,format=yuv420p[v]",
        "-map", "[v]"
    ]
    if _media_has_audio(src):
        cmd += ["-map", "0:a:0"]
    else:
        dur = duration or 5.0
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={dur}", "-map", "2:a:0"]
    cmd += ["-r", "30", *video_encode_args(crf), "-c:a", "aac", "-b:a", "160k", "-shortest", str(target)]
    code, so, se = run(cmd)
    if code == 0 and target.exists():
        return target
    return None


def _outro_cache_key(src, overlay):
    try:
        parts = []
        for p in (Path(src), overlay):
            st = p.stat()
            parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    except Exception:
        return None

def _outro_segment_with_copyright(src, target):
    # The outro template + copyright overlay are the same shared settings-level
    # assets for every project/render, so the rendered result is identical every
    # time until one of those two files actually changes. Cache it once (keyed by
    # mtime+size of both inputs) instead of re-running a full libx264/videotoolbox
    # encode of the outro on every single export.
    overlay = CONFIG / "outro_copyright.png"
    cache_dir = CONFIG / "cache"
    cache_key = _outro_cache_key(src, overlay) if overlay.exists() else None
    cached = cache_dir / f"outro_{cache_key}.mp4" if cache_key else None
    if cached and cached.exists():
        try:
            shutil.copyfile(str(cached), str(target))
            return target
        except Exception:
            pass
    out = None
    if overlay.exists() and ffmpeg_has_filter("overlay"):
        out = _overlay_png_on_video(src, overlay, target)
    if not out:
        out = _normalized_segment(src, 0, None, target)
    if out and cached:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(out), str(cached))
        except Exception:
            pass
    return out


def _intro_text_alpha_expr(index, count, duration):
    # Postupný fade out: spodní text mizí nejdřív, hlavní nadpis nejpozději.
    duration = float(duration or 5.0)
    count = max(1, int(count or 1))
    fade_len = 0.85
    gap = 0.35
    last_start = max(0.6, duration - fade_len - 0.25)
    start = max(0.4, last_start - (count - 1 - index) * gap)
    end = min(duration, start + fade_len)
    return _ffmpeg_expr_escape(f"if(lt(t,{start:.3f}),1,if(lt(t,{end:.3f}),({end:.3f}-t)/{max(0.001,end-start):.3f},0))")


def _intro_screen_preview_svg(pid):
    cfg = project_config(pid)
    items = _intro_screen_items(cfg)
    out = project_dir(pid) / "work" / "intro_screen.svg"
    frame = _intro_template_frame(cfg, project_dir(pid) / "work" / "intro_screen_bg.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    esc = lambda x: html.escape(str(x), quote=True)
    if frame and frame.exists():
        b64 = base64.b64encode(frame.read_bytes()).decode("ascii")
        bg = f'<image href="data:image/jpeg;base64,{b64}" x="0" y="0" width="1920" height="1080" preserveAspectRatio="xMidYMid slice"/>'
    else:
        bg = '<rect width="1920" height="1080" fill="#020617"/>'
    texts = []
    scale = 1.5
    for idx, it in enumerate(items[:8]):
        font = esc(_safe_intro_font_name(it.get("font", "Inter")))
        x = int(float(it.get("x", 120)) * scale)
        y = int(float(it.get("y", 240)) * scale)
        size = int(float(it.get("size", 36)) * scale)
        start_fade = max(0.2, 5.0 - 1.0 - idx * 0.25)
        text = esc(it["text"])
        weight = it["weight"]
        texts.append(f'<text x="{x}" y="{y}" font-family="{font}, Helvetica, Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="#f8fafc" stroke="#020617" stroke-width="5" paint-order="stroke fill">{text}<animate attributeName="opacity" values="1;1;0" keyTimes="0;{start_fade/5.0:.3f};1" dur="5s" fill="freeze" /></text>')
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  {bg}
  <rect width="1920" height="1080" fill="#000000" opacity="0.18"/>
  {''.join(texts)}
</svg>"""
    out.write_text(svg, encoding="utf-8")
    cfg.setdefault("analysis", {})["intro_screen_preview"] = str(out)
    save_project(cfg)
    return out

def _generate_intro_screen(pid):
    _intro_screen_preview_svg(pid)
    return project_config(pid)


def _drawtext_filters_for_intro(cfg, work, duration=5.0):
    filters = []
    items = _intro_screen_items(cfg)[:8]
    count = len(items)
    for i, it in enumerate(items):
        tf = work / f"intro_text_{i}.txt"
        tf.write_text(str(it.get("text", "")), encoding="utf-8")
        fontfile = _fontfile_for_intro(it.get("font", "Inter"))
        opts = []
        if fontfile:
            opts.append(f"fontfile={_ffmpeg_filter_value_escape(fontfile)}")
        opts.append(f"textfile={_ffmpeg_filter_value_escape(tf.as_posix())}")
        opts.append("fontcolor=white")
        scale = 1.5
        opts.append(f"fontsize={int(float(it.get('size', 36)) * scale)}")
        opts.append(f"x={int(float(it.get('x', 120)) * scale)}")
        opts.append(f"y={int(float(it.get('y', 240)) * scale)}")
        opts.append("borderw=3")
        opts.append("bordercolor=black@0.75")
        # Pozor: u subprocessu nejsou shellové uvozovky odstraňovány.
        # Hodnoty pro drawtext proto nesmějí být obalené apostrofy; jinak FFmpeg
        # spadne na parsování textfile/fontfile/alpha. Čárky v alpha výrazu escapujeme.
        opts.append(f"alpha={_intro_text_alpha_expr(i, count, duration)}")
        filters.append("drawtext=" + ":".join(opts))
    return filters



def _media_has_audio(path):
    try:
        return bool(ffprobe(path).get("audio"))
    except Exception:
        return False

def _intro_template_duration(cfg, fallback=5.0):
    intro = load_settings().get("intro_template")
    if intro and Path(intro).exists():
        try:
            d = float(ffprobe(intro).get("duration") or 0)
            if d > 0.25:
                return d
        except Exception:
            pass
    return float(fallback or 5.0)

def _intro_overlay_svg(cfg, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    items = _intro_screen_items(cfg)[:8]
    esc = lambda x: html.escape(str(x), quote=True)
    texts = []
    scale = 1.5
    for it in items:
        font = esc(_safe_intro_font_name(it.get("font", "Inter")))
        x = int(float(it.get("x", 120)) * scale)
        y = int(float(it.get("y", 240)) * scale)
        size = int(float(it.get("size", 36)) * scale)
        weight = it["weight"]
        text = esc(it["text"])
        texts.append(f'<text x="{x}" y="{y}" font-family="{font}, Helvetica, Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="#f8fafc" stroke="#020617" stroke-width="5" paint-order="stroke fill">{text}</text>')
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  <rect width="1920" height="1080" fill="#000000" opacity="0.18"/>
  {''.join(texts)}
</svg>"""
    target.write_text(svg, encoding="utf-8")
    return target

def _svg_to_png_with_quicklook(svg_path, png_path):
    svg_path = Path(svg_path); png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = png_path.parent / (png_path.stem + "_ql")
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.glob("*"):
        try: old.unlink()
        except Exception: pass
    try:
        code, so, se = run(["qlmanage", "-t", "-s", "1280", "-o", str(tmp), str(svg_path)])
        candidates = list(tmp.glob("*.png")) + list(tmp.glob("*.jpg")) + list(tmp.glob("*.jpeg"))
        if code == 0 and candidates:
            data = candidates[0].read_bytes()
            png_path.write_bytes(data)
            return png_path
    except Exception:
        pass
    return None

def _title_screen_segment(cfg, target, duration=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    work = target.parent
    settings = load_settings()
    intro = settings.get("intro_template")
    intro_exists = bool(intro and Path(intro).exists())
    duration = _intro_template_duration(cfg, 8.0) if duration is None else float(duration or 8.0)
    duration = max(0.5, duration)
    base_filters = [
        "scale=1920:1080:force_original_aspect_ratio=decrease",
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        "format=yuv420p",
    ]

    # Prefer a backend-generated transparent PNG overlay. This does not depend on
    # the browser remembering to upload a canvas and avoids drawtext/libass/QuickLook.
    backend_overlay = _intro_overlay_png_backend(cfg, work / "intro_overlay_backend.png")
    if intro_exists and backend_overlay and Path(backend_overlay).exists() and ffmpeg_has_filter("overlay"):
        out = _overlay_png_on_video(intro, backend_overlay, target, duration, crf="22")
        if out:
            return out

    # Fallback: transparent PNG overlay generated by the browser from the live title designer.
    browser_overlay = _browser_intro_overlay_path(cfg.get("id", ""))
    if intro_exists and browser_overlay.exists() and ffmpeg_has_filter("overlay"):
        out = _overlay_png_on_video(intro, browser_overlay, target, duration, crf="22")
        if out:
            return out

    # Prefer ASS subtitles over the real intro video. This keeps the moving
    # AA intro as the real background and avoids the old QuickLook SVG->PNG path
    # which destroyed transparency and produced the gray title card.
    if intro_exists and (ffmpeg_has_filter("subtitles") or ffmpeg_has_filter("ass")):
        ass = _intro_ass_file(cfg, work / "intro_titles.ass", duration)
        sub_filter = "subtitles" if ffmpeg_has_filter("subtitles") else "ass"
        vf = f"{','.join(base_filters)},{sub_filter}='{_ffmpeg_filter_path(ass)}'"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(intro), "-t", str(duration),
               "-vf", vf, "-map", "0:v:0"]
        if _media_has_audio(intro):
            cmd += ["-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}", "-map", "1:a:0"]
        cmd += ["-r", "30", *video_encode_args("22"), "-c:a", "aac", "-b:a", "160k", "-shortest", str(target)]
        code, so, se = run(cmd)
        if code == 0 and target.exists():
            return target

    overlay_svg = _intro_overlay_svg(cfg, work / "intro_overlay.svg")
    overlay_png = None  # disabled: qlmanage rendered transparent SVGs with a gray background on macOS
    fade_start = max(0.1, duration - 1.2)
    if intro_exists and overlay_png and Path(overlay_png).exists() and ffmpeg_has_filter("overlay"):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(intro), "-loop", "1", "-i", str(overlay_png), "-t", str(duration),
               "-filter_complex", f"[0:v]{','.join(base_filters)}[bg];[1:v]scale=1920:1080,format=rgba,fade=t=out:st={fade_start:.3f}:d=1.0:alpha=1[ov];[bg][ov]overlay=0:0:format=auto,format=yuv420p[v]",
               "-map", "[v]"]
        if _media_has_audio(intro):
            cmd += ["-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}", "-map", "2:a:0"]
        cmd += ["-r", "30", *video_encode_args("22"), "-c:a", "aac", "-b:a", "160k", "-shortest", str(target)]
        code, so, se = run(cmd)
        if code == 0 and target.exists():
            return target

    if intro_exists and ffmpeg_has_filter("drawtext"):
        overlay = ",".join([*base_filters, *_drawtext_filters_for_intro(cfg, work, duration)])
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(intro), "-t", str(duration),
               "-filter_complex", f"[0:v]{overlay}[v]", "-map", "[v]"]
        if _media_has_audio(intro):
            cmd += ["-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}", "-map", "1:a:0"]
        cmd += ["-r", "30", *video_encode_args("22"), "-c:a", "aac", "-b:a", "160k", "-shortest", str(target)]
        code, so, se = run(cmd)
        if code == 0 and target.exists():
            return target

    if intro_exists:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(intro), "-t", str(duration),
               "-vf", ",".join(base_filters), "-r", "30", *video_encode_args("22"), "-c:a", "aac", "-b:a", "160k", "-shortest", str(target)]
        code, so, se = run(cmd)
        if code == 0 and target.exists():
            return target

    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "lavfi", "-i", f"color=c=0f172a:s=1920x1080:d={duration}",
           "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}",
           "-vf", ",".join(base_filters), "-map", "0:v:0", "-map", "1:a:0",
           "-r", "30", *video_encode_args("22"), "-c:a", "aac", "-b:a", "160k", "-shortest", str(target)]
    code, so, se = run(cmd)
    if code != 0 or not target.exists():
        raise RuntimeError((se or so or "Vytvoření vstupní obrazovky selhalo")[-1500:])
    return target

def _compose_edit_master(pid):
    """Create the single editable master video from the multitrack timeline.

    Important: this function is the source of truth for step 4. It must not just
    concatenate the original files in a fixed order. It walks the timeline and
    switches source according to the manual cut points:
      - intro/title screen first,
      - speaker video from real_start,
      - replacement video only over its timeline interval,
      - gallery video from discussion_start to the end,
      - outro at the end.
    """
    cfg = project_config(pid)
    cfg = _recover_project_files_from_folder(cfg)
    files = cfg.get("files", {})
    cuts = cfg.get("cuts", {})
    speaker = files.get("speaker_video")
    gallery = files.get("gallery_video")
    replacement = files.get("replacement_video")
    if not speaker or not Path(speaker).exists():
        raise RuntimeError("Chybí hlavní video")

    media = cfg.setdefault("analysis", {}).setdefault("media", {})
    for role, src in [("speaker_video", speaker), ("gallery_video", gallery), ("replacement_video", replacement)]:
        if src and Path(src).exists():
            try:
                media[role] = ffprobe(src)
            except Exception:
                pass

    speaker_dur = float(media.get("speaker_video", {}).get("duration") or _media_duration(speaker) or 0)
    gallery_dur = float(media.get("gallery_video", {}).get("duration") or _media_duration(gallery) or 0) if gallery and Path(gallery).exists() else 0
    replacement_dur = float(media.get("replacement_video", {}).get("duration") or _media_duration(replacement) or 0) if replacement and Path(replacement).exists() else 0
    try:
        print(f"Compose vstupy: speaker={bool(speaker and Path(speaker).exists())} {speaker_dur:.3f}s, replacement={bool(replacement and Path(replacement).exists())} {replacement_dur:.3f}s, gallery={bool(gallery and Path(gallery).exists())} {gallery_dur:.3f}s", flush=True)
    except Exception:
        pass

    real = max(0.0, _seconds(cuts.get("real_start"), 0))
    if real < 0.001:
        real = 0.0

    # The manual "main_end" can be stale/too early from older builds. It must not
    # cut away the gallery or the replacement interval. Use it only when it is
    # clearly after all required switch points.
    base_end = speaker_dur
    # Galerie má po naznačeném přechodu pokračovat až do svého konce.
    # Proto ji nesmíme ořezat délkou hlavního videa ani starým main_end.
    if gallery and Path(gallery).exists() and gallery_dur > 0:
        base_end = gallery_dur

    rs = _seconds(cuts.get("replacement_start"), -1)
    if rs < 0 and replacement and Path(replacement).exists() and replacement_dur > 0:
        # Last-resort fallback from analysis markers, if the frontend has not saved the cut.
        for m in cfg.get("analysis", {}).get("markers", []) or []:
            if m.get("kind") == "replacement":
                rs = _seconds(m.get("start"), -1)
                break
    if rs < 0 and replacement and Path(replacement).exists() and replacement_dur > 0:
        # Stejný odhad jako ve střihačské ose, aby se vizuálně naznačená stopa propsala i do masteru.
        rs = min(473.0, max(0.0, base_end * 0.10))
    re_ = rs + replacement_dur if replacement_dur > 0 and rs >= 0 else -1

    ds = _seconds(cuts.get("discussion_start") if cuts.get("discussion_start") is not None else cuts.get("gallery_start"), -1)
    if ds < 0:
        for m in cfg.get("analysis", {}).get("markers", []) or []:
            if m.get("kind") in ("discussion_start", "gallery_start", "gallery"):
                ds = _seconds(m.get("start"), -1)
                break
    if ds < 0 and gallery and Path(gallery).exists() and gallery_dur > 0:
        # Střihačská osa ukazuje výchozí marker i před ručním uložením.
        # Když uživatel nechá výchozí pozici, musí se galerie přesto zapojit.
        ds = min(4792.0, max(real + 1.0, base_end * 0.85))
    if gallery and Path(gallery).exists() and gallery_dur > 0 and ds >= 0:
        # Přechod do galerie je čas na společné ose. Galerie má od tohoto bodu
        # pokračovat až do svého konce, i když je hlavní video kratší nebo je main_end starý.
        base_end = max(base_end, gallery_dur)
        # FULL AUTO někdy pracovalo se starším/stale markerem z předchozího projektu
        # (typicky 4792 s), který u kratších nahrávek ležel až za koncem časové osy.
        # V takovém případě se vůbec nevytvořil segment galerie a výsledek zůstal
        # na hlavním videu. Pokud marker leží mimo použitelný rozsah, vrať ho na
        # stejný bezpečný odhad, jaký ukazuje střihačská osa pro aktuální délku.
        if ds >= base_end - 0.25:
            stale_ds = ds
            ds = min(4792.0, max(real + 1.0, base_end * 0.85))
            if ds >= base_end - 0.25:
                ds = max(real + 1.0, base_end - min(300.0, max(30.0, base_end * 0.15)))
            try:
                print(f"Galerie: marker {stale_ds:.3f}s byl mimo délku osy {base_end:.3f}s, používám {ds:.3f}s", flush=True)
            except Exception:
                pass
            cuts["discussion_start"] = ds
            cuts["gallery_start"] = ds

    try:
        print(f"Compose strihy: real={real:.3f}s, replacement={rs:.3f}-{re_:.3f}s, gallery={ds:.3f}s, endCandidate={base_end:.3f}s", flush=True)
    except Exception:
        pass

    required_until = real + 1
    if rs >= 0 and re_ > rs:
        required_until = max(required_until, min(base_end, re_))
    if ds >= 0:
        required_until = max(required_until, min(base_end, ds + 1))

    main_end_raw = _seconds(cuts.get("main_end"), -1)
    if gallery and Path(gallery).exists() and gallery_dur > 0:
        # Po přechodu do galerie se má pokračovat do konce galerie.
        end = base_end
    elif main_end_raw > required_until + 0.25:
        end = min(base_end, main_end_raw)
    else:
        end = base_end
    if end <= real + 0.25:
        end = base_end
    end = max(real + 1, min(end, base_end))

    work = project_dir(pid) / "work" / "edit_master"
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("*.mp4"):
        try: old.unlink()
        except Exception: pass
    segments = []
    manifest = []

    settings = load_settings()
    intro = settings.get('intro_template')
    outro = settings.get('outro_template')

    def add_manifest(label, src, src_start, dur, target):
        # output_start/output_end are measured in the newly composed edit master.
        # They are essential for FULL AUTO, because the silence-removal step must
        # not delete structural video segments such as the replacement clip,
        # gallery, intro or outro just because their audio is quiet.
        out_start = 0.0
        for item in manifest:
            try:
                out_start = max(out_start, float(item.get("output_end", out_start) or out_start))
            except Exception:
                pass
        duration_value = None if dur is None else round(float(dur), 3)
        out_end = None if duration_value is None else round(out_start + float(duration_value), 3)
        manifest.append({
            "label": label,
            "source": str(src),
            "source_start": round(float(src_start or 0), 3),
            "duration": duration_value,
            "output_start": round(out_start, 3),
            "output_end": out_end,
            "target": str(target),
        })

    # Úvodní znělka se nesmí objevit dvakrát. Titulky se skládají přímo nad intro.
    if cfg.get("meta"):
        seg = _title_screen_segment(cfg, work / f"{len(segments):03d}_intro_titles.mp4")
        if seg:
            segments.append(seg)
            add_manifest("intro_titles", intro or "generated", 0, float(ffprobe(seg).get("duration") or 0), seg)
    elif intro and Path(intro).exists():
        seg = _normalized_segment(intro, 0, None, work / f"{len(segments):03d}_intro.mp4")
        if seg:
            segments.append(seg)
            add_manifest("intro", intro, 0, None, seg)

    # Explicit timeline composition. This is deliberately not a generic
    # interval classifier anymore: FULL AUTO must produce the same visual order
    # every time and must not silently skip replacement/gallery.
    def add_timeline_segment(label, src, src_start, duration):
        if not src or not Path(src).exists():
            return False
        try:
            src_start = max(0.0, float(src_start or 0.0))
            duration = float(duration or 0.0)
        except Exception:
            return False
        if duration <= 0.25:
            return False
        try:
            src_total = float(ffprobe(src).get("duration") or 0)
            if src_total > 0:
                duration = min(duration, max(0.0, src_total - src_start))
        except Exception:
            pass
        if duration <= 0.25:
            return False
        target = work / f"{len(segments):03d}_{label}.mp4"
        seg = _normalized_segment(src, src_start, duration, target)
        if seg:
            segments.append(seg)
            add_manifest(label, src, src_start, duration, seg)
            return True
        return False

    cursor = real

    # Part 1: main/speaker video from the trim point to the replacement start
    # or gallery switch, whichever comes first.
    next_switches = [x for x in [rs if rs >= real else None, ds if ds >= real else None] if isinstance(x, (int, float))]
    first_stop = min(next_switches) if next_switches else end
    if first_stop > cursor + 0.25:
        add_timeline_segment("speaker", speaker, cursor, first_stop - cursor)
        cursor = first_stop

    # Part 2: replacement clip. It is an overlay in the UI, but in the resulting
    # video it visually replaces the main track for the exact placed interval.
    if replacement and Path(replacement).exists() and replacement_dur > 0 and rs >= real and rs < end:
        if cursor < rs - 0.25:
            add_timeline_segment("speaker", speaker, cursor, rs - cursor)
        repl_dur = min(replacement_dur, max(0.0, end - rs))
        if ds >= 0 and ds > rs:
            repl_dur = min(repl_dur, max(0.0, ds - rs))
        target = work / f"{len(segments):03d}_replacement.mp4"
        seg = _overlay_timeline_segment(speaker, replacement, rs, 0.0, repl_dur, target, prefer_overlay_audio=True)
        if seg:
            segments.append(seg)
            add_manifest("replacement", replacement, 0.0, repl_dur, seg)
            try:
                print(f"Replacement překryv: base={rs:.3f}s overlay=0.000s dur={repl_dur:.3f}s", flush=True)
            except Exception:
                pass
        else:
            add_timeline_segment("replacement", replacement, 0.0, repl_dur)
        cursor = max(cursor, rs + repl_dur)

    # Part 3: speaker between replacement and gallery.
    if ds >= real and ds < end:
        if cursor < ds - 0.25:
            add_timeline_segment("speaker", speaker, cursor, ds - cursor)
            cursor = ds
        # Part 4: gallery from the defined global timeline point to the end.
        # Zoom gallery is parallel to speaker, so source_start stays equal to
        # the global timeline time. Clamp only to avoid invalid FFmpeg requests.
        gallery_start = max(0.0, min(float(ds), max(0.0, gallery_dur - 0.5)))
        gallery_available = max(0.0, gallery_dur - gallery_start)
        gallery_duration = min(max(0.0, end - ds), gallery_available)
        target = work / f"{len(segments):03d}_gallery.mp4"
        seg = _overlay_timeline_segment(speaker, gallery, ds, gallery_start, gallery_duration, target, prefer_overlay_audio=True)
        if seg:
            segments.append(seg)
            add_manifest("gallery", gallery, gallery_start, gallery_duration, seg)
            try:
                print(f"Galerie překryv: base={ds:.3f}s overlay={gallery_start:.3f}s dur={gallery_duration:.3f}s", flush=True)
            except Exception:
                pass
        else:
            add_timeline_segment("gallery", gallery, gallery_start, gallery_duration)
        cursor = end
    else:
        if cursor < end - 0.25:
            add_timeline_segment("speaker", speaker, cursor, end - cursor)
            cursor = end

    # Safety diagnostics: if the UI/project claims replacement or gallery but no
    # segment was created, print why instead of failing silently.
    labels_now = [str(x.get("label")) for x in manifest]
    if replacement and Path(replacement).exists() and replacement_dur > 0 and "replacement" not in labels_now:
        try:
            print(f"VAROVANI: replacement segment nevznikl; rs={rs:.3f}, re={re_:.3f}, end={end:.3f}, dur={replacement_dur:.3f}", flush=True)
        except Exception:
            pass
    if gallery and Path(gallery).exists() and gallery_dur > 0 and "gallery" not in labels_now:
        try:
            print(f"VAROVANI: gallery segment nevznikl; ds={ds:.3f}, end={end:.3f}, gallery_dur={gallery_dur:.3f}", flush=True)
        except Exception:
            pass

    if outro and Path(outro).exists():
        seg = _outro_segment_with_copyright(outro, work / f"{len(segments):03d}_outro.mp4")
        if seg:
            segments.append(seg)
            add_manifest("outro", outro, 0, None, seg)

    if not segments:
        raise RuntimeError("Nevznikl žádný segment pro pracovní video")
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{x.as_posix()}'\n" for x in segments), encoding="utf-8")
    out = project_dir(pid) / "work" / "edit_master.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(out)]
    code, so, se = run(cmd)
    if code != 0 or not out.exists():
        raise RuntimeError((se or so or "Spojení pracovního videa selhalo")[-1500:])

    (work / "segment_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        print("Edit master segmenty:", ", ".join(str(x.get("label")) for x in manifest), flush=True)
    except Exception:
        pass

    media["edit_master_video"] = ffprobe(out)
    # Persist possibly corrected gallery/replacement cut points. This is mainly
    # for FULL AUTO, so all following steps use the same timeline that was used
    # for the actual master composition.
    cfg.setdefault("cuts", {})["replacement_start"] = rs
    cfg.setdefault("cuts", {})["replacement_end"] = re_
    if ds >= 0:
        cfg.setdefault("cuts", {})["discussion_start"] = ds
        cfg.setdefault("cuts", {})["gallery_start"] = ds
    cfg.setdefault("files", {})["edit_master_original"] = str(out)
    cfg.setdefault("files", {})["edit_master_video"] = str(out)
    cfg.setdefault("analysis", {})["edit_master"] = {
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration": float(media["edit_master_video"].get("duration") or 0),
        "real_start": real,
        "timeline_end": end,
        "segments": len(segments),
        "segment_manifest": manifest,
        "replacement_start": rs,
        "replacement_end": re_,
        "discussion_start": ds,
        "intro_screen": bool(cfg.get("meta")),
        "included_outro": bool(outro and Path(outro).exists()),
    }
    cfg.setdefault("analysis", {})["edit_silences"] = []
    cfg.setdefault("analysis", {})["edit_manual_cuts"] = []
    save_project(cfg)
    return cfg

def _export_edit_audio(pid):
    cfg = project_config(pid)
    src = _edit_master_source(cfg)
    out = project_dir(pid) / "output" / f"{safe_name(cfg.get('name','ArboChat'))}_audio.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-vn", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)]
    code, so, se = run(cmd)
    if code != 0 or not out.exists():
        raise RuntimeError((se or so or "Export WAV selhal")[-1200:])
    return out


def _import_edit_audio(pid):
    cfg = project_config(pid)
    files = cfg.setdefault("files", {})
    src_video = files.get("edit_master_original") or files.get("edit_master_video")
    if not src_video or not Path(src_video).exists():
        raise RuntimeError("Nejdřív vytvoř pracovní video tlačítkem Upravit video")
    audio = Path(_choose_macos("file", "Vyber upravenou zvukovou stopu"))
    if not audio.exists():
        raise RuntimeError("Vybraný audio soubor neexistuje")
    out = project_dir(pid) / "work" / "edit_master_with_imported_audio.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(src_video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)]
    code, so, se = run(cmd)
    if code != 0 or not out.exists():
        raise RuntimeError((se or so or "Import zvuku selhal")[-1200:])
    files["imported_audio"] = str(audio)
    files["edit_master_video"] = str(out)
    cfg.setdefault("analysis", {}).setdefault("media", {})["edit_master_video"] = ffprobe(out)
    cfg.setdefault("analysis", {})["edit_master"] = {**cfg.setdefault("analysis", {}).get("edit_master", {}), "imported_audio_active": True}
    save_project(cfg)
    return cfg


def _adjust_edit_audio(pid):
    cfg = project_config(pid)
    src = _edit_master_source(cfg)
    out = project_dir(pid) / "work" / "edit_master_audio_adjusted.mp4"
    # Komprese + vyrovnání hlasitosti pro mluvené slovo. Video kopírujeme beze změny.
    af = "acompressor=threshold=-18dB:ratio=3:attack=20:release=250,loudnorm=I=-16:TP=-1.5:LRA=11"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
        "-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out)
    ]
    code, so, se = run(cmd)
    if code != 0 or not out.exists():
        raise RuntimeError((se or so or "Úprava zvuku selhala")[-1500:])
    cfg.setdefault("files", {})["edit_master_video"] = str(out)
    cfg.setdefault("analysis", {}).setdefault("media", {})["edit_master_video"] = ffprobe(out)
    cfg.setdefault("analysis", {})["edit_master"] = {**cfg.setdefault("analysis", {}).get("edit_master", {}), "audio_adjusted_at": datetime.datetime.now().isoformat(timespec="seconds"), "duration": float(ffprobe(out).get("duration") or 0)}
    save_project(cfg)
    return cfg


def _manifest_segments_with_output_ranges(cfg):
    """Return edit-master manifest items with output_start/output_end filled.

    Older manifests did not store output_start. In that case we reconstruct it
    cumulatively. This lets the optimizer protect whole structural segments.
    """
    manifest = list((cfg.get("analysis", {}).get("edit_master", {}) or {}).get("segment_manifest", []) or [])
    result = []
    cursor = 0.0
    for item in manifest:
        it = dict(item)
        try:
            dur = float(it.get("duration") or 0)
        except Exception:
            dur = 0.0
        if it.get("output_start") is None:
            it["output_start"] = round(cursor, 3)
        try:
            start = float(it.get("output_start") or cursor)
        except Exception:
            start = cursor
        if it.get("output_end") is None:
            it["output_end"] = round(start + max(0.0, dur), 3)
        try:
            cursor = max(cursor, float(it.get("output_end") or start + dur))
        except Exception:
            cursor = start + dur
        result.append(it)
    return result


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(float(a1), float(b1)) - max(float(a0), float(b0)))


def _optimize_edit_video(pid):
    cfg = project_config(pid)
    src = _edit_master_source(cfg)
    silences = analyze_silences(src)

    # FULL AUTO must never delete structural visual segments. The short
    # replacement video and gallery can have little or no audio, so a pure
    # silence detector would mark them as removable and they would disappear
    # from the final product. Protect these manifest sections from automatic
    # silence removal. Manual cuts still remain available to the user.
    protected_labels = {"intro", "intro_titles", "replacement", "gallery", "outro"}
    protected = []
    for it in _manifest_segments_with_output_ranges(cfg):
        label = str(it.get("label") or "")
        if label in protected_labels:
            try:
                protected.append((float(it.get("output_start") or 0), float(it.get("output_end") or 0), label))
            except Exception:
                pass

    filtered = []
    skipped = []
    for m in silences:
        st = float(m.get("start", 0) or 0)
        en = float(m.get("end", st) or st)
        hit = next(((a,b,label) for a,b,label in protected if _overlap(st, en, a, b) > 0.25), None)
        if hit:
            skipped.append({**m, "protected_by": hit[2]})
            continue
        filtered.append(m)

    cfg.setdefault("analysis", {})["edit_silences"] = [
        {**m, "kind": "edit_silence", "label": "Ticho k odstranění", "text": f"Ticho nad 3 s: {seconds_to_time(float(m.get('start',0)))}–{seconds_to_time(float(m.get('end',0)))}"}
        for m in filtered
    ]
    cfg.setdefault("analysis", {})["edit_silences_protected"] = skipped
    try:
        print(f"Optimalizace tich: nalezeno {len(silences)}, chráněno {len(skipped)}, k odstranění {len(filtered)}", flush=True)
    except Exception:
        pass
    save_project(cfg)
    return cfg


def _delete_marked_silences(pid):
    cfg = project_config(pid)
    src = _edit_master_source(cfg)
    analysis = cfg.get("analysis", {})
    silences = list(analysis.get("edit_silences", []) or [])
    manual = list(analysis.get("edit_manual_cuts", []) or [])
    silences = silences + manual
    if not silences:
        cfg.setdefault("analysis", {})["edit_silences"] = []
        cfg.setdefault("analysis", {})["edit_manual_cuts"] = []
        save_project(cfg)
        return cfg
    dur = float(ffprobe(src).get("duration") or 0)
    keep = []
    cursor = 0.0
    for m in sorted(silences, key=lambda x: float(x.get("start", 0) or 0)):
        st = max(0.0, float(m.get("start", 0) or 0))
        en = min(dur, float(m.get("end", st) or st))
        if st > cursor + 0.25:
            keep.append((cursor, st))
        cursor = max(cursor, en)
    if cursor < dur - 0.25:
        keep.append((cursor, dur))
    if not keep:
        raise RuntimeError("Po vymazání tich by nezbyl žádný obraz")
    work = project_dir(pid) / "work" / "silence_cut"
    work.mkdir(parents=True, exist_ok=True)
    segs = []
    for old in work.glob("*.mp4"):
        try: old.unlink()
        except Exception: pass
    for i, (a,b) in enumerate(keep):
        seg = _normalized_segment(src, a, b-a, work / f"{i:03d}_keep.mp4")
        if seg: segs.append(seg)
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{x.as_posix()}'\n" for x in segs), encoding="utf-8")
    out = project_dir(pid) / "work" / "edit_master_silence_cut.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(out)]
    code, so, se = run(cmd)
    if code != 0 or not out.exists():
        raise RuntimeError((se or so or "Mazání tich selhalo")[-1500:])
    cfg.setdefault("files", {})["edit_master_video"] = str(out)
    cfg.setdefault("analysis", {}).setdefault("media", {})["edit_master_video"] = ffprobe(out)
    cfg.setdefault("analysis", {})["edit_master"] = {**cfg.setdefault("analysis", {}).get("edit_master", {}), "silences_removed_at": datetime.datetime.now().isoformat(timespec="seconds"), "duration": float(ffprobe(out).get("duration") or 0)}
    cfg.setdefault("analysis", {})["edit_silences"] = []
    cfg.setdefault("analysis", {})["edit_manual_cuts"] = []
    save_project(cfg)
    return cfg


def _render_full_hd_segment(src, start, duration, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    seg_dur = _segment_duration(src, start, duration)
    if seg_dur <= 0.25:
        return None
    dur_args = ["-t", _ffmpeg_time(seg_dur)]
    vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    base = ["ffmpeg", "-y", "-hide_banner", "-ss", _ffmpeg_time(start), "-i", str(src), *dur_args]
    if _media_has_audio(src):
        cmd = base + [
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
            "-r", "30", *video_encode_args("20"),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", "-movflags", "+faststart", str(target)
        ]
    else:
        cmd = base + [
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={_ffmpeg_time(seg_dur)}",
            "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
            "-r", "30", *video_encode_args("20"),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", "-movflags", "+faststart", str(target)
        ]
    code, so, se = run(cmd)
    if code != 0 or not target.exists():
        raise RuntimeError((se or so or f"Nelze vytvořit Full HD segment {target.name}")[-1500:])
    return target



def _concat_full_hd_segments(segments, final, work):
    """Concat already-normalized Full HD mp4 segments into one final mp4 with audio embedded."""
    final = Path(final); work = Path(work)
    final.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    if not segments:
        raise RuntimeError("Nevznikl žádný Full HD segment pro finální video")
    if len(segments) == 1:
        shutil.copyfile(str(segments[0]), str(final))
        return final
    concat = work / "concat_full_hd.txt"
    concat.write_text("".join(f"file '{Path(x).as_posix()}'\n" for x in segments), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(final)]
    code, so, se = run(cmd)
    if code != 0 or not final.exists():
        raise RuntimeError((se or so or "Spojení Full HD finálního videa selhalo")[-1500:])
    return final

def _write_arbochat_summary(cfg, outdir):
    meta = cfg.get("meta", {}) or {}
    files = cfg.get("files", {}) or {}
    entries, plain = parse_transcript(files.get("transcript_file")) if files.get("transcript_file") else ([], "")
    text = re.sub(r"\s+", " ", plain or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text) if text else []
    picked = []
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 45:
            continue
        if sent in picked:
            continue
        picked.append(sent)
        if len(picked) >= 5:
            break
    if not picked and text:
        picked = [text[:650].strip()]
    if not picked:
        picked = ["Přepis nebyl dostupný, proto nelze automaticky sestavit obsahový přehled."]
    lines = [
        f"ArboChat: {meta.get('topic') or cfg.get('name') or ''}",
        f"Řečník: {meta.get('speaker') or 'neuvedeno'}",
        f"Datum: {meta.get('date') or 'neuvedeno'}",
        "",
        "Stručný přehled obsahu:",
    ]
    for sent in picked:
        lines.append(f"- {sent}")
    lines.extend(["", "Celý textový přepis:", text or "Přepis nebyl dostupný."])
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{safe_name(cfg.get('name','ArboChat'))}_prehled.txt"
    out.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return out


# --- Sample for social networks: a short vertical (9:16) highlight reel for
# Facebook/LinkedIn made of a generated title card plus a handful of cutaways
# from the speaker and gallery tracks, joined with subtle crossfades. ---

def _vertical_normalized_segment(src, start, duration, target, crf="21"):
    """Same idea as _normalized_segment, but center-cropped to 9:16 for social clips."""
    target.parent.mkdir(parents=True, exist_ok=True)
    seg_dur = _segment_duration(src, start, duration)
    if seg_dur <= 0.25:
        return None
    dur_args = ["-t", _ffmpeg_time(seg_dur)]
    base = ["ffmpeg", "-y", "-hide_banner", "-ss", _ffmpeg_time(start), "-i", str(src), *dur_args]
    vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920,setsar=1,format=yuv420p"
    if _media_has_audio(src):
        cmd = base + [
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", vf, "-r", "30", *video_encode_args(crf),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(target)
        ]
    else:
        cmd = base + [
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={_ffmpeg_time(seg_dur)}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", vf, "-r", "30", *video_encode_args(crf),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(target)
        ]
    code, so, se = run(cmd)
    if code != 0 or not target.exists():
        raise RuntimeError((se or so or f"Nelze vytvořit vertikální záběr {target.name}")[-1500:])
    return target


def _social_title_overlay_png(cfg, target, width=1080, height=1920):
    """Transparent PNG with the title card text, rendered via Pillow.

    ffmpeg's drawtext/subtitles filters need libfreetype/libass, which aren't
    part of every ffmpeg build (this app already hits that with the horizontal
    intro card and falls back to a Pillow-rendered PNG composited with the
    always-available `overlay` filter — same approach here, just vertical).
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    meta = cfg.get("meta", {}) or {}
    topic = str(meta.get("topic") or cfg.get("name") or "ArboChat").strip()
    speaker = str(meta.get("speaker") or "").strip()
    episode = str(meta.get("episode_number") or "").strip()

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def font_for(size):
        fontfile = _fontfile_for_intro("Inter")
        try:
            if fontfile and Path(fontfile).exists():
                return ImageFont.truetype(fontfile, size=size)
        except Exception:
            pass
        for cand in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            try:
                if Path(cand).exists():
                    return ImageFont.truetype(cand, size=size)
            except Exception:
                pass
        return ImageFont.load_default()

    def fit_lines(text, start_size, min_size, max_width):
        # Shrink to fit on one line; if still too wide even at min_size, wrap
        # into two lines split near the middle word boundary.
        size = start_size
        while size > min_size:
            font = font_for(size)
            w = draw.textbbox((0, 0), text, font=font)[2]
            if w <= max_width:
                return [(text, font)]
            size -= 2
        font = font_for(min_size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width or " " not in text:
            return [(text, font)]
        words = text.split(" ")
        best_split, best_diff = 1, None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            diff = abs(len(a) - len(b))
            if best_diff is None or diff < best_diff:
                best_split, best_diff = i, diff
        return [(" ".join(words[:best_split]), font), (" ".join(words[best_split:]), font)]

    def draw_centered(text, y, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((width - w) / 2, y), text, font=font, fill=fill,
                   stroke_width=max(2, int(font.size * 0.06)), stroke_fill=(2, 6, 23, 225))
        return h

    max_w = int(width * 0.86)
    topic_lines = fit_lines(topic or (cfg.get("name") or "ArboChat"), 64, 38, max_w)
    line_gap = 14
    block_h = (0 if not episode else 46 + 24)
    block_h += sum(f.size + 10 for _, f in topic_lines) + line_gap * max(0, len(topic_lines) - 1)
    block_h += (0 if not speaker else 40 + 24)

    band_pad = 46
    band_top = int(height * 0.5 - (block_h + band_pad * 2) / 2)
    band_bottom = band_top + block_h + band_pad * 2
    draw.rectangle([0, band_top, width, band_bottom], fill=(6, 12, 26, 150))

    y = band_top + band_pad
    if episode:
        y += draw_centered(f"ARBOCHAT #{episode}", y, font_for(46), (74, 222, 128, 255))
        y += 24
    for text, font in topic_lines:
        y += draw_centered(text, y, font, (248, 250, 252, 255))
        y += line_gap
    if speaker:
        y += 10
        draw_centered(speaker, y, font_for(40), (203, 213, 225, 255))

    img.save(target)
    return target if target.exists() else None


def _social_title_card(cfg, target, duration=4.0, background_src=None, background_time=None):
    """Vertical title card: ArboChat episode number, topic/title and speaker name.

    Background is a blurred, slowly zoomed still frame taken from the speaker
    video when available (feels tied to the actual recording instead of a flat
    color), otherwise falls back to a plain brand-colored background. Text is
    composited from a Pillow-rendered PNG (see _social_title_overlay_png).
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    work = target.parent

    frame_path = None
    if background_src and Path(background_src).exists():
        candidate = work / "social_title_bg.jpg"
        bt = 0.0 if background_time is None else max(0.0, float(background_time))
        cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", _ffmpeg_time(bt), "-i", str(background_src),
               "-frames:v", "1", "-vf", "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920",
               "-q:v", "3", str(candidate)]
        code, so, se = run(cmd)
        if code == 0 and candidate.exists():
            frame_path = candidate

    frames_n = max(1, int(round(float(duration) * 30)))
    if frame_path:
        inputs = ["-loop", "1", "-i", str(frame_path), "-t", str(duration)]
        # Blurred + darkened + desaturated backdrop with a slow, subtle zoom
        # (professional "Ken Burns" feel rather than a static or flashy card).
        bg_vf = (
            "scale=1080:1920,gblur=sigma=22,eq=brightness=-0.18:saturation=0.75,"
            f"zoompan=z='min(zoom+0.0006,1.05)':d={frames_n}:s=1080x1920:fps=30,format=yuv420p"
        )
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0f172a:s=1080x1920:d={duration}"]
        bg_vf = "format=yuv420p"

    overlay_png = _social_title_overlay_png(cfg, work / "social_title_overlay.png")
    if overlay_png and ffmpeg_has_filter("overlay"):
        filter_complex = (
            f"[0:v]{bg_vf}[bg];[1:v]scale=1080:1920,format=rgba[ov];"
            "[bg][ov]overlay=0:0:format=auto,format=yuv420p[v]"
        )
        cmd = ["ffmpeg", "-y", "-hide_banner", *inputs, "-loop", "1", "-i", str(overlay_png), "-t", str(duration),
               "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}",
               "-filter_complex", filter_complex,
               "-map", "[v]", "-map", "2:a:0",
               "-r", "30", *video_encode_args("21"), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
               "-shortest", "-movflags", "+faststart", str(target)]
    else:
        # No text renderer available at all: still produce a usable (textless) card
        # instead of failing the whole highlight reel.
        cmd = ["ffmpeg", "-y", "-hide_banner", *inputs,
               "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}",
               "-vf", bg_vf, "-map", "0:v:0", "-map", "1:a:0",
               "-r", "30", *video_encode_args("21"), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
               "-shortest", "-movflags", "+faststart", str(target)]
    code, so, se = run(cmd)
    if code != 0 or not target.exists():
        raise RuntimeError((se or so or "Vytvoření titulní karty selhalo")[-1500:])
    return target


def _xfade_concat(clip_paths, final, work, transition_duration=0.5, transitions=None):
    """Join already-normalized same-format clips with crossfades (xfade/acrossfade)
    instead of a hard cut. `transitions` is a list of one transition name per join
    (length len(clip_paths)-1); defaults to a plain crossfade throughout.
    """
    clip_paths = [Path(c) for c in clip_paths if c]
    if not clip_paths:
        raise RuntimeError("Nevznikl žádný záběr pro spojení")
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    if len(clip_paths) == 1:
        shutil.copyfile(str(clip_paths[0]), str(final))
        return final

    durations = [float(ffprobe(c).get("duration") or 0) for c in clip_paths]
    transitions = list(transitions or [])
    while len(transitions) < len(clip_paths) - 1:
        transitions.append("fade")

    t = float(transition_duration)
    t = max(0.15, min(t, min(durations) * 0.35))

    inputs = []
    for c in clip_paths:
        inputs += ["-i", str(c)]

    filter_parts = []
    prev_v, prev_a = "0:v", "0:a"
    cum = durations[0]
    for i in range(1, len(clip_paths)):
        trans = transitions[i - 1]
        offset = max(0.0, cum - t)
        vout, aout = f"v{i}", f"a{i}"
        filter_parts.append(f"[{prev_v}][{i}:v]xfade=transition={trans}:duration={t:.3f}:offset={offset:.3f}[{vout}]")
        filter_parts.append(f"[{prev_a}][{i}:a]acrossfade=d={t:.3f}[{aout}]")
        prev_v, prev_a = vout, aout
        cum = cum + durations[i] - t

    cmd = ["ffmpeg", "-y", "-hide_banner", *inputs,
           "-filter_complex", ";".join(filter_parts),
           "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
           "-r", "30", *video_encode_args("20"), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(final)]
    code, so, se = run(cmd)
    if code != 0 or not final.exists():
        raise RuntimeError((se or so or "Spojení vzorku pro sociální sítě selhalo")[-1500:])
    return final


def _generate_social_clip(pid):
    """Build a ~1 minute vertical (9:16) highlight reel for Facebook/LinkedIn:
    a generated title card, a speaker close-up, a few presentation cutaways from
    later in the talk, and (if available) several gallery/discussion cutaways,
    joined with subtle, professional crossfades rather than hard cuts.
    """
    cfg = project_config(pid)
    cfg = _recover_project_files_from_folder(cfg)
    files = cfg.get("files", {})
    cuts = cfg.get("cuts", {})
    speaker = files.get("speaker_video")
    gallery = files.get("gallery_video")
    if not speaker or not Path(speaker).exists():
        raise RuntimeError("Chybí hlavní video pro vzorek pro sociální sítě")

    speaker_dur = float(ffprobe(speaker).get("duration") or 0)
    gallery_dur = float(ffprobe(gallery).get("duration") or 0) if gallery and Path(gallery).exists() else 0

    real = max(0.0, _seconds(cuts.get("real_start"), 0))
    discussion = _seconds(cuts.get("discussion_start") if cuts.get("discussion_start") is not None else cuts.get("gallery_start"), -1)
    main_end = discussion if (discussion >= 0 and discussion > real) else speaker_dur
    if speaker_dur > 0:
        main_end = min(main_end, speaker_dur)
    main_span = max(0.0, main_end - real)

    work = project_dir(pid) / "work" / "social_clip"
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("*"):
        try: old.unlink()
        except Exception: pass

    clips = []
    transitions = []

    def add(path_or_none, transition="fade"):
        if path_or_none:
            if clips:
                transitions.append(transition)
            clips.append(path_or_none)

    # 1) Generated title card: episode number, topic and speaker.
    bg_time = real + min(4.0, main_span * 0.15) if main_span > 0 else 0.0
    add(_social_title_card(cfg, work / "000_title.mp4", duration=4.0, background_src=speaker, background_time=bg_time))

    # 2) Speaker close-up near the start of the talk.
    hero_start = real + (1.5 if main_span > 3 else 0)
    hero_dur = min(9.0, max(3.0, main_span * 0.25)) if main_span > 0 else 3.0
    add(_vertical_normalized_segment(speaker, hero_start, hero_dur, work / "001_speaker.mp4"), "fade")

    # 3) A few presentation cutaways spread across the rest of the talk.
    remaining_start = hero_start + hero_dur + 1.0
    remaining_span = max(0.0, main_end - remaining_start)
    presentation_count = 3 if remaining_span > 15 else (2 if remaining_span > 8 else (1 if remaining_span > 3 else 0))
    presentation_trans = ["dissolve", "smoothleft", "fade"]
    if presentation_count:
        slot = remaining_span / presentation_count
        clip_dur = min(7.0, max(2.5, slot * 0.55))
        for i in range(presentation_count):
            t0 = remaining_start + slot * i + slot * 0.2
            add(_vertical_normalized_segment(speaker, t0, clip_dur, work / f"{len(clips):03d}_presentation.mp4"),
                presentation_trans[i % len(presentation_trans)])

    # 4) Gallery/discussion cutaways sampled across the gallery clip's own timeline.
    has_gallery = bool(gallery and Path(gallery).exists() and gallery_dur > 1.0)
    if has_gallery:
        gallery_count = 4 if gallery_dur > 40 else (3 if gallery_dur > 20 else (2 if gallery_dur > 8 else 1))
        gallery_trans = ["smoothright", "dissolve", "fade", "smoothleft"]
        margin = min(3.0, gallery_dur * 0.08)
        usable = max(0.0, gallery_dur - margin * 2)
        slot = usable / gallery_count if gallery_count else 0
        clip_dur = min(6.0, max(2.5, slot * 0.6)) if slot else 0
        for i in range(gallery_count):
            t0 = margin + slot * i + slot * 0.15
            add(_vertical_normalized_segment(gallery, t0, clip_dur, work / f"{len(clips):03d}_gallery.mp4"),
                gallery_trans[i % len(gallery_trans)])
    elif remaining_span > 3 and presentation_count < 3:
        # No gallery footage: one more presentation cutaway so the reel still runs close to a minute.
        t0 = remaining_start + remaining_span * 0.7
        add(_vertical_normalized_segment(speaker, t0, min(7.0, max(2.5, remaining_span * 0.25)),
            work / f"{len(clips):03d}_presentation.mp4"), "dissolve")

    # 5) Optional closing brand card from the shared outro template.
    settings = load_settings()
    outro = settings.get("outro_template")
    if outro and Path(outro).exists():
        outro_dur = min(3.5, float(ffprobe(outro).get("duration") or 3.5))
        add(_vertical_normalized_segment(outro, 0, outro_dur, work / f"{len(clips):03d}_outro.mp4"), "fade")

    if len(clips) < 2:
        raise RuntimeError("Nepodařilo se sestavit dostatek záběrů pro vzorek pro sociální sítě")

    final = project_dir(pid) / "output" / f"{safe_name(cfg.get('name','ArboChat'))}_socialni_sit_vertical.mp4"
    _xfade_concat(clips, final, work, transition_duration=0.5, transitions=transitions)

    cfg.setdefault("files", {})["social_clip_video"] = str(final)
    cfg.setdefault("analysis", {})["social_clip"] = {
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration": float(ffprobe(final).get("duration") or 0),
        "clips": len(clips),
        "has_gallery": has_gallery,
    }
    save_project(cfg)
    return cfg

def _render_project(pid):
    cfg = project_config(pid)
    files = cfg.get("files", {})
    cuts = cfg.get("cuts", {})

    # Primární workflow v4.3: finální video vzniká z jedné pracovní master stopy
    # vytvořené tlačítkem Upravit video. Master už obsahuje intro i outro.
    master = files.get("edit_master_video")
    if master and Path(master).exists():
        info = ffprobe(master)
        total = float(info.get("duration") or 0)
        start = max(0.0, float(cuts.get("edit_master_start") or 0))
        end = float(cuts.get("edit_master_end") or total or 0)
        if end <= start + 0.25:
            end = total
        outdir = project_dir(pid) / "output"
        work = project_dir(pid) / "work" / "render_master"
        outdir.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        final = outdir / f"{safe_name(cfg.get('name','ArboChat'))}_final.mp4"

        # Finální výstup je vždy jedno MP4: obraz + aktivní zvuková stopa dohromady.
        # Nevracíme samostatný WAV; ten zůstává jen pracovní export v sekci 4.
        segments = []
        # The master is built by _compose_edit_master/_delete_marked_silences from
        # segments that are already normalized to this exact Full HD/h264/aac spec
        # and stream-copy concatenated, so when no extra trim is requested here it
        # is byte-for-byte already the target format. Re-encoding it again in that
        # case was pure wasted work (a full second libx264 pass over the whole
        # video every render). Skip straight to using it as the segment; the copy
        # path in _concat_full_hd_segments below then makes this a plain file copy
        # instead of a re-encode. Any real trim (start/end inside the master) still
        # goes through the frame-accurate re-encode path unchanged.
        if start <= 0.01 and end >= total - 0.01 and _is_full_hd_h264_aac(info):
            master_seg = Path(master)
        else:
            master_seg = _render_full_hd_segment(master, start, end - start if end else None, work / "000_master_full_hd.mp4")
        if master_seg:
            segments.append(master_seg)

        settings = load_settings()
        outro = settings.get("outro_template")
        edit_info = cfg.get("analysis", {}).get("edit_master", {}) or {}
        # Nově vytvořený master už outro obsahuje. Starší mastery bez příznaku ho dostanou až tady,
        # aby ve finálním videu nechyběla znělka s copyrightem.
        if outro and Path(outro).exists() and not bool(edit_info.get("included_outro")):
            outro_seg = _render_full_hd_segment(outro, 0, None, work / "001_outro_full_hd.mp4")
            if outro_seg:
                segments.append(outro_seg)

        _concat_full_hd_segments(segments, final, work)
        if not final.exists():
            raise RuntimeError("Finální Full HD render z pracovní stopy selhal")
        try:
            finfo = ffprobe(final)
            print(f"Final video: {int(finfo.get('width') or 0)}x{int(finfo.get('height') or 0)}, {float(finfo.get('duration') or 0):.3f}s", flush=True)
        except Exception:
            pass
        summary = _write_arbochat_summary(cfg, outdir)
        return {"ok": True, "output": str(final), "file": final.name, "summary": str(summary), "summary_file": summary.name}
    speaker = files.get("speaker_video")
    gallery = files.get("gallery_video")
    replacement = files.get("replacement_video")
    if not speaker or not Path(speaker).exists():
        raise RuntimeError("Chybí hlavní video")
    info = ffprobe(speaker); total = float(info.get("duration") or 0)
    real = _seconds(cuts.get("real_start"), 0)
    rs = _seconds(cuts.get("replacement_start"), -1)
    re_ = _seconds(cuts.get("replacement_end"), -1)
    ds = _seconds(cuts.get("discussion_start"), -1)
    outdir = project_dir(pid) / "output"; work = project_dir(pid) / "work" / "render"
    outdir.mkdir(parents=True, exist_ok=True); work.mkdir(parents=True, exist_ok=True)
    segments = []

    settings = load_settings()
    for label, src, start, end in []:
        pass

    def add_segment(src, start, end, name):
        if not src or not Path(src).exists(): return
        if end is not None and end <= start + 0.2: return
        target = work / f"{len(segments):03d}_{name}.mp4"
        dur_args = [] if end is None else ["-t", _ffmpeg_time(max(0.25, end - start))]
        cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", _ffmpeg_time(start), "-i", str(src), *dur_args,
               "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p", "-r", "30", *video_encode_args("20"), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(target)]
        code, so, se = run(cmd)
        if code != 0 or not target.exists():
            raise RuntimeError((se or so or f"Nelze vytvořit segment {name}")[-1200:])
        segments.append(target)

    # Optional intro and outro, not personalized yet in this rewrite.
    intro = settings.get("intro_template")
    outro = settings.get("outro_template")
    if intro and Path(intro).exists():
        add_segment(intro, 0, None, "intro")

    first_end = rs if rs > real else (ds if ds > real else None)
    add_segment(speaker, real, first_end, "speaker_a")
    if replacement and Path(replacement).exists() and rs >= 0 and re_ > rs:
        add_segment(replacement, 0, None, "replacement")
        add_segment(speaker, re_, ds if ds > re_ else None, "speaker_b")
    if gallery and Path(gallery).exists() and ds >= 0:
        add_segment(gallery, ds, None, "gallery_discussion")
    if outro and Path(outro).exists():
        add_segment(outro, 0, None, "outro")

    if not segments:
        raise RuntimeError("Nevznikl žádný segment k renderu")
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments), encoding="utf-8")
    raw = work / "fallback_raw_concat.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(raw)]
    code, so, se = run(cmd)
    if code != 0 or not raw.exists():
        raise RuntimeError((se or so or "Finální spojení selhalo")[-1500:])

    # I záložní starší render musí skončit jako jedno Full HD MP4 s audiem uvnitř.
    final = outdir / f"{safe_name(cfg.get('name','ArboChat'))}_render_full_hd.mp4"
    _render_full_hd_segment(raw, 0, None, final)
    if not final.exists():
        raise RuntimeError("Finální Full HD render selhal")
    summary = _write_arbochat_summary(cfg, outdir)
    return {"ok": True, "output": str(final), "file": final.name, "summary": str(summary), "summary_file": summary.name}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def parse_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, download=False):
        path = Path(path)
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = _content_type(path)
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start_s, _, end_s = range_header.replace("bytes=", "", 1).partition("-")
            start = int(start_s or 0)
            end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_error(416)
                return
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            if download:
                self.send_header("Content-Disposition", f"attachment; filename=\"{path.name}\"")
            self.end_headers()
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(1024 * 512, remaining))
                        if not chunk: break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename=\"{path.name}\"")
        self.end_headers()
        try:
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if path == "/api/status":
                code, so, se = run(["ffmpeg", "-version"])
                settings = load_settings()
                return self.send_json({"ok": True, "ffmpeg": code == 0, "settings": settings, "terms": load_terms(), "version": "React ViteCutTimeline"})

            if path == "/api/projects":
                items = []
                for p in sorted(PROJECTS.glob("*/project.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                    try: items.append(json.loads(p.read_text(encoding="utf-8")))
                    except Exception: pass
                return self.send_json({"ok": True, "projects": items})

            if path == "/api/project":
                return self.send_json(project_config(q.get("id", [""])[0]))

            if path == "/api/choose_directory":
                pid = q.get("project", [""])[0]
                if not pid: return self.send_json(_json_error("Chybí projekt"), 400)
                folder = _choose_macos("folder", "Vyber složku se soubory ArboChatu")
                cfg = project_config(pid)
                cfg.setdefault("source", {})["folder"] = folder
                cfg.setdefault("files", {}).update(scan_arbochat_folder(folder))
                save_project(cfg)
                return self.send_json({"ok": True, "project": cfg})

            if path == "/api/choose_project_file":
                pid = q.get("project", [""])[0]
                role = q.get("role", [""])[0]
                allowed = {"speaker_video", "gallery_video", "replacement_video", "transcript_file"}
                if not pid:
                    return self.send_json(_json_error("Chybí projekt"), 400)
                if role not in allowed:
                    return self.send_json(_json_error("Neznámý typ projektového souboru"), 400)
                src = Path(_choose_macos("file", "Vyber soubor pro projekt"))
                cfg = project_config(pid)
                cfg.setdefault("files", {})[role] = str(src)
                save_project(cfg)
                return self.send_json({"ok": True, "project": cfg})

            if path == "/api/choose_common_file":
                role = q.get("role", [""])[0]
                if role not in COMMON_FILES: return self.send_json(_json_error("Neznámý typ společného souboru"), 400)
                src = Path(_choose_macos("file", "Vyber společný soubor"))
                ext = src.suffix.lower()
                filename = COMMON_FILES[role]
                if role == "topics_file" and ext == ".xlsx":
                    filename = "topics.xlsx"
                target = CONFIG / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                settings = load_settings(); settings[role] = str(target); save_settings(settings)
                return self.send_json({"ok": True, "settings": settings})

            if path == "/api/frame_preview":
                return self.send_file(_make_preview_frame(q.get("project", [""])[0], float(q.get("time", ["0"])[0]), q.get("role", [None])[0]))

            if path == "/api/play_before":
                return self.send_file(_make_video_preview(q.get("project", [""])[0], float(q.get("time", ["0"])[0]), float(q.get("seconds", ["5"])[0]), q.get("role", [None])[0]))

            if path == "/api/audio_preview":
                return self.send_file(_make_audio_preview(q.get("project", [""])[0], float(q.get("start", ["0"])[0]), float(q.get("end", ["8"])[0])))

            if path == "/api/waveform_peaks":
                return self.send_json(_waveform_peaks(q.get("project", [""])[0], int(q.get("width", ["1800"])[0]), q.get("role", [None])[0]))

            if path == "/api/source_video":
                pid = q.get("project", [""])[0]
                role = q.get("role", ["speaker_video"])[0]
                cfg = project_config(pid)
                src = cfg.get("files", {}).get(role)
                if not src: src = _source_for_project(cfg)
                return self.send_file(src)

            if path == "/api/export_edit_audio":
                return self.send_file(_export_edit_audio(q.get("project", [""])[0]), download=True)

            if path == "/api/generate_intro_screen":
                return self.send_json({"ok": True, "project": _generate_intro_screen(q.get("project", [""])[0])})

            if path == "/api/intro_screen_preview":
                return self.send_file(_intro_screen_preview_svg(q.get("project", [""])[0]))

            if path == "/api/intro_background_frame":
                return self.send_file(_intro_background_frame(q.get("project", [""])[0]))

            if path == "/api/choose_edit_audio":
                return self.send_json({"ok": True, "project": _import_edit_audio(q.get("project", [""])[0])})

            # Static React build
            rel = path.lstrip("/") or "index.html"
            file_path = WEB / rel
            if file_path.is_dir():
                file_path = file_path / "index.html"
            if not file_path.exists():
                file_path = WEB / "index.html"
            return self.send_file(file_path)
        except Exception as e:
            return self.send_json(_json_error(str(e), 500), 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            data = self.parse_json_body()
            if path == "/api/create_project":
                name = data.get("name") or "ArboChat"
                pid = safe_name(name) + "_" + uuid.uuid4().hex[:6]
                d = project_dir(pid)
                for sub in ["input", "work", "output"]:
                    (d / sub).mkdir(parents=True, exist_ok=True)
                cfg = {"id": pid, "name": name, "files": {}, "source": {}, "meta": {"date": extract_project_date(name), "episode_number": "", "topic": "", "speaker": "", "intro_layout": intro_layout_defaults()}, "cuts": {"real_start": 0}, "analysis": {}}
                save_project(cfg)
                return self.send_json({"ok": True, "project": cfg})

            if path == "/api/save_project":
                pid = data.get("id")
                cfg = project_config(pid)
                if "name" in data and data.get("name"):
                    cfg["name"] = data["name"]
                if "meta" in data and isinstance(data.get("meta"), dict):
                    cfg["meta"] = _merge_preserving_existing(cfg.get("meta", {}), data.get("meta", {}))
                if "cuts" in data and isinstance(data.get("cuts"), dict):
                    cfg["cuts"] = _merge_preserving_existing(cfg.get("cuts", {}), data.get("cuts", {}))
                if "files" in data and isinstance(data.get("files"), dict):
                    cfg["files"] = _merge_preserving_existing(cfg.get("files", {}), data.get("files", {}), preserve_paths=True)
                if "source" in data and isinstance(data.get("source"), dict):
                    cfg["source"] = _merge_preserving_existing(cfg.get("source", {}), data.get("source", {}))
                if "analysis" in data and isinstance(data.get("analysis"), dict) and data.get("analysis"):
                    # Never replace existing media/markers with an empty stale object from the browser.
                    cfg["analysis"] = _merge_preserving_existing(cfg.get("analysis", {}), data.get("analysis", {}))
                _recover_project_files_from_folder(cfg)
                save_project(cfg)
                return self.send_json({"ok": True, "project": cfg})

            if path == "/api/save_intro_overlay":
                pid = data.get("project") or data.get("id")
                overlay = data.get("overlay") or data.get("data_url") or ""
                _save_intro_overlay_png(pid, overlay)
                return self.send_json({"ok": True})

            if path == "/api/save_intro_layout_defaults":
                layout = data.get("layout") or {}
                if not isinstance(layout, dict):
                    layout = {}
                settings = load_settings()
                settings["intro_layout_defaults"] = layout
                save_settings(settings)
                return self.send_json({"ok": True, "settings": settings})

            if path == "/api/analyze":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _analyze_project(pid)})

            if path == "/api/finalize_edit":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _finalize_edit(pid)})

            if path == "/api/prepare_edit_video":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _compose_edit_master(pid)})

            if path == "/api/optimize_edit_video":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _optimize_edit_video(pid)})

            if path == "/api/delete_marked_silences":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _delete_marked_silences(pid)})

            if path == "/api/adjust_edit_audio":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _adjust_edit_audio(pid)})

            if path == "/api/render":
                pid = data.get("project") or data.get("id")
                return self.send_json(_render_project(pid))

            if path == "/api/generate_social_clip":
                pid = data.get("project") or data.get("id")
                return self.send_json({"ok": True, "project": _generate_social_clip(pid)})

            return self.send_json(_json_error("Neznámý endpoint", 404), 404)
        except Exception as e:
            return self.send_json(_json_error(str(e), 500), 500)


def main():
    print("ArboChat Cutter React / ViteCutTimeline")
    print("Open http://127.0.0.1:8787")
    ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()


if __name__ == "__main__":
    main()
