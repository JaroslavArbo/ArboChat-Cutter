export async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { ok:false, error:text }; }
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

export const fmt = (seconds) => {
  const t = Math.max(0, Number(seconds || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = Math.floor(t % 60);
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
};

export const parseTime = (value, fallback = 0) => {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'number') return value;
  const raw = String(value).trim().replace(',', '.');
  let m = raw.match(/^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/);
  if (m) return Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
  m = raw.match(/^(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/);
  if (m) return Number(m[1]) * 60 + Number(m[2]);
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
};
