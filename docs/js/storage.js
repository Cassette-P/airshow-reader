/**
 * storage.js - localStorage persistence
 * Handles play progress (v1), blur listening mode, theme preference, and language mode
 */

export const PROGRESS_STORAGE_KEY = 'airshow_reader_progress_v1';
export const BLUR_STORAGE_KEY = 'airshow_blur_mode';
export const THEME_STORAGE_KEY = 'airshow_theme';
export const LANG_STORAGE_KEY = 'airshow_lang_mode';

export function getStoredProgressMap() {
  try {
    const raw = localStorage.getItem(PROGRESS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    console.warn('[AIRSHOW][Progress] Failed to read localStorage:', e);
    return {};
  }
}

export function getEpisodeProgress(showId, guid) {
  if (!showId || !guid) return null;
  const map = getStoredProgressMap();
  return map[`${showId}::${guid}`] || null;
}

export function saveEpisodeProgress(showId, guid, position, duration, forceCompleted = false) {
  if (!showId || !guid || !Number.isFinite(position) || position < 0) return null;
  try {
    const map = getStoredProgressMap();
    const key = `${showId}::${guid}`;
    const prev = map[key] || {};
    const dur = (Number.isFinite(duration) && duration > 0) ? duration : (prev.duration || 0);

    let isDone = forceCompleted || prev.completed || false;
    if (!isDone && dur > 0) {
      if ((position / dur) >= 0.95 || (dur > 60 && (dur - position) < 30)) {
        isDone = true;
      }
    }

    const updated = {
      position: Math.round(position * 10) / 10,
      duration: Math.round(dur * 10) / 10,
      completed: isDone,
      updatedAt: Date.now()
    };
    map[key] = updated;
    localStorage.setItem(PROGRESS_STORAGE_KEY, JSON.stringify(map));
    return updated;
  } catch (e) {
    console.warn('[AIRSHOW][Progress] Failed to save progress:', e);
    return null;
  }
}

export function getStoredBlurMode() {
  try {
    const saved = localStorage.getItem(BLUR_STORAGE_KEY) || 'off';
    return ['off', 'zh', 'both'].includes(saved) ? saved : 'off';
  } catch (e) {
    console.warn('[AIRSHOW][Blur] Failed to read localStorage:', e);
    return 'off';
  }
}

export function setStoredBlurMode(mode) {
  if (!['off', 'zh', 'both'].includes(mode)) mode = 'off';
  try {
    localStorage.setItem(BLUR_STORAGE_KEY, mode);
  } catch (e) {
    console.warn('[AIRSHOW][Blur] Failed to save localStorage:', e);
  }
  return mode;
}

export function getStoredTheme() {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY);
  } catch (e) {
    console.warn('[AIRSHOW][Theme] Failed to read localStorage:', e);
    return null;
  }
}

export function setStoredTheme(theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (e) {
    console.warn('[AIRSHOW][Theme] Failed to save localStorage:', e);
  }
}

export function getStoredLangMode() {
  try {
    return localStorage.getItem(LANG_STORAGE_KEY) || 'bilingual';
  } catch (e) {
    console.warn('[AIRSHOW][Lang] Failed to read localStorage:', e);
    return 'bilingual';
  }
}

export function setStoredLangMode(mode) {
  try {
    localStorage.setItem(LANG_STORAGE_KEY, mode);
  } catch (e) {
    console.warn('[AIRSHOW][Lang] Failed to save localStorage:', e);
  }
}
