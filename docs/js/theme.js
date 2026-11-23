/**
 * theme.js - Theme toggle, dynamic favicon, theme-color meta & PWA detection
 */

import { state, elements } from './state.js';
import { getStoredTheme, setStoredTheme, getStoredLangMode, setStoredLangMode } from './storage.js';
import { lightenHexColor, colorToRgba } from './utils.js';

export function detectPwaMode() {
  const isStandalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || window.navigator.standalone === true;
  if (isStandalone) {
    document.documentElement.classList.add('is-pwa');
  }
  return isStandalone;
}

export function updateThemeColorMeta(theme) {
  const themeColor = theme === 'dark' ? '#151412' : '#FAF8F4';
  const themeColorMeta = document.querySelector('meta[name="theme-color"]');
  if (themeColorMeta) {
    themeColorMeta.setAttribute('content', themeColor);
  }
}

export function updateDynamicFavicon(bgHex) {
  const faviconEl = document.getElementById('dynamic-favicon');
  if (!faviconEl) return;
  const svgXml = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="7" fill="${bgHex}" />
  <rect x="6" y="12" width="2.6" height="8" rx="1.3" fill="#ffffff" />
  <rect x="10.2" y="8" width="2.6" height="16" rx="1.3" fill="#ffffff" />
  <rect x="14.4" y="10.5" width="2.6" height="11" rx="1.3" fill="#ffffff" />
  <rect x="19.2" y="9.5" width="7" height="2.4" rx="1.2" fill="#ffffff" opacity="0.92" />
  <rect x="19.2" y="14.8" width="7" height="2.4" rx="1.2" fill="#ffffff" opacity="0.92" />
  <rect x="19.2" y="20.1" width="4.8" height="2.4" rx="1.2" fill="#ffffff" opacity="0.78" />
</svg>`;
  faviconEl.href = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svgXml)}`;
}

export function applyShowTheme(show) {
  const id = show ? show.id : 'airshow';
  let lightAccent = '#0A2540';
  let darkAccent = '#78B2DF';

  if (id === 'tal' || (show && show.accent_color && show.accent_color.toLowerCase().includes('2d6a4f'))) {
    lightAccent = '#2D6A4F';
    darkAccent = '#74C69D';
  } else if (id === 'sinica' || (show && show.accent_color && show.accent_color.toLowerCase().includes('9b2226'))) {
    lightAccent = '#9B2226';
    darkAccent = '#DF7A74';
  } else if (id === 'airshow') {
    lightAccent = '#0A2540';
    darkAccent = '#78B2DF';
  } else if (show && show.accent_color) {
    lightAccent = show.accent_color;
    darkAccent = lightenHexColor(show.accent_color);
  }

  document.documentElement.style.setProperty('--show-accent-light', lightAccent);
  document.documentElement.style.setProperty('--show-accent-dark', darkAccent);
  document.documentElement.style.setProperty('--show-accent', lightAccent);

  const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
  const isDark = currentTheme === 'dark';
  const activeAccent = isDark ? darkAccent : lightAccent;
  const tintAlpha = isDark ? 0.15 : 0.10;
  const subtleAlpha = isDark ? 0.07 : 0.05;

  document.documentElement.style.setProperty('--accent-primary', activeAccent);
  document.documentElement.style.setProperty('--accent-tint', colorToRgba(activeAccent, tintAlpha));
  document.documentElement.style.setProperty('--accent-subtle', colorToRgba(activeAccent, subtleAlpha));

  updateDynamicFavicon(lightAccent);
}

export function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  setStoredTheme(theme);
  updateThemeColorMeta(theme);

  if (elements.iconSun && elements.iconMoon) {
    if (theme === 'dark') {
      elements.iconSun.style.display = 'block';
      elements.iconMoon.style.display = 'none';
    } else {
      elements.iconSun.style.display = 'none';
      elements.iconMoon.style.display = 'block';
    }
  }

  if (state.currentShow) {
    applyShowTheme(state.currentShow);
  }
}

export function toggleTheme() {
  const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
  const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
  setTheme(nextTheme);
}

export function initTheme() {
  const savedTheme = getStoredTheme();
  const systemPrefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = savedTheme || (systemPrefersDark ? 'dark' : 'light');
  setTheme(theme);
}

export function setLanguageMode(mode) {
  state.currentLangMode = mode;
  document.body.setAttribute('data-lang-mode', mode);
  setStoredLangMode(mode);

  document.querySelectorAll('.segment-btn').forEach(btn => {
    if (btn.getAttribute('data-mode') === mode) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });

  if (elements.mobileLangBtn) {
    const charMap = { bilingual: '双', zh: '中', en: '英' };
    const titleMap = { bilingual: '双语模式', zh: '仅中文', en: '仅英文' };
    elements.mobileLangBtn.textContent = charMap[mode] || '双';
    elements.mobileLangBtn.setAttribute('title', `当前模式：${titleMap[mode] || mode} (点击切换)`);
  }
}

export function cycleLanguageMode() {
  const modes = ['bilingual', 'zh', 'en'];
  const currentIndex = modes.indexOf(state.currentLangMode);
  const nextIndex = (currentIndex + 1) % modes.length;
  setLanguageMode(modes[nextIndex]);
}

export function initLanguageMode() {
  const savedMode = getStoredLangMode();
  setLanguageMode(savedMode);

  document.querySelectorAll('.segment-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.getAttribute('data-mode');
      if (mode) setLanguageMode(mode);
    });
  });

  if (elements.mobileLangBtn) {
    elements.mobileLangBtn.addEventListener('click', cycleLanguageMode);
  }
}
