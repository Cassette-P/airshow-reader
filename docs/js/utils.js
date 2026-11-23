/**
 * utils.js - Pure utility functions
 * escapeHtml, time formatting, binary search, speaker color hash (% 8), color transforms
 */

export function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatTime(seconds) {
  if (isNaN(seconds) || seconds < 0) return '00:00';
  const totalSecs = Math.floor(seconds);
  const hrs = Math.floor(totalSecs / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  const pad = n => String(n).padStart(2, '0');
  if (hrs > 0) {
    return `${hrs}:${pad(mins)}:${pad(secs)}`;
  }
  return `${pad(mins)}:${pad(secs)}`;
}

export function formatDurationZh(seconds) {
  if (isNaN(seconds) || seconds < 0) return '0秒';
  const totalSecs = Math.floor(seconds);
  const hrs = Math.floor(totalSecs / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  if (hrs > 0) {
    return `${hrs}小时${mins}分${secs}秒`;
  }
  return `${mins}分${secs}秒`;
}

export function formatDateZh(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr.slice(0, 10);
    return `${d.getUTCFullYear()}年${d.getUTCMonth() + 1}月${d.getUTCDate()}日`;
  } catch {
    return String(isoStr).slice(0, 10);
  }
}

/**
 * Requirement 一 & 四: 8 dynamic speaker groups with modulo % 8
 */
export function getSpeakerCssClass(speaker) {
  if (!speaker) return 'spk-dyn-0';
  const s = speaker.toLowerCase().trim();
  if (s.includes('jon') || s.includes('ostrower')) return 'spk-jon';
  if (s.includes('brian') || s.includes('sumers')) return 'spk-brian';
  if (s.includes('brett') || s.includes('snyder')) return 'spk-brett';
  if (s.includes('kaiser') || s.includes('kuo')) return 'spk-kaiser';
  if (s.includes('ira') || s.includes('glass')) return 'spk-ira';

  let hash = 0;
  for (let i = 0; i < s.length; i++) {
    hash = ((hash << 5) - hash) + s.charCodeAt(i);
    hash |= 0;
  }
  const idx = Math.abs(hash) % 8;
  return `spk-dyn-${idx}`;
}

export function binarySearchActiveIndex(items, currentTime) {
  if (!items || items.length === 0) return -1;
  let low = 0;
  let high = items.length - 1;
  let result = -1;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if (items[mid].start_sec <= currentTime) {
      result = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return result;
}

export function lightenHexColor(hex) {
  let c = hex.replace('#', '');
  if (c.length === 3) c = c.split('').map(x => x + x).join('');
  const num = parseInt(c, 16);
  if (isNaN(num)) return '#78B2DF';
  let r = (num >> 16) & 255;
  let g = (num >> 8) & 255;
  let b = num & 255;
  r = Math.round(r + (255 - r) * 0.55);
  g = Math.round(g + (255 - g) * 0.55);
  b = Math.round(b + (255 - b) * 0.55);
  return `rgb(${r}, ${g}, ${b})`;
}

export function colorToRgba(colorStr, alpha) {
  if (!colorStr) return `rgba(10, 37, 64, ${alpha})`;
  colorStr = colorStr.trim();
  if (colorStr.startsWith('#')) {
    let c = colorStr.replace('#', '');
    if (c.length === 3) c = c.split('').map(x => x + x).join('');
    const num = parseInt(c, 16);
    if (isNaN(num)) return `rgba(10, 37, 64, ${alpha})`;
    const r = (num >> 16) & 255;
    const g = (num >> 8) & 255;
    const b = num & 255;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  const match = colorStr.match(/rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
  if (match) {
    return `rgba(${match[1]}, ${match[2]}, ${match[3]}, ${alpha})`;
  }
  return `rgba(10, 37, 64, ${alpha})`;
}
