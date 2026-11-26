/**
 * main.js - Main entry point & application orchestrator
 * Bootstraps modules, registers Service Worker, manages URL Hash routing & top-level events
 */

import { state, elements, initElements, showIndexCache, episodeCache } from './state.js';
import { getEpisodeProgress } from './storage.js';
import { fetchShows, fetchShowIndex, fetchEpisodeDetail } from './api.js';
import { detectPwaMode, initTheme, initLanguageMode, applyShowTheme, toggleTheme } from './theme.js';
import {
  initPlayerEvents,
  initMediaSessionActionHandlers,
  updateMediaSession,
  updatePlayPauseIcons,
  exitSentenceLoop,
  throttleSaveProgress,
  setProgressUpdateCallback,
  updateMastheadPlayhead
} from './player.js';
import {
  renderArticleMeta,
  renderExecutiveSummary,
  renderHighlights,
  renderKnowledgeCards,
  resetKnowledgeCards,
  renderTranscript,
  initBlurMode,
  clearRevealedStates,
  applySearchFilter,
  initReaderEvents
} from './reader.js';
import {
  renderPodcastTabs,
  updatePodcastTabsActive,
  renderEpisodeArchive,
  updateEpisodeProgressIndicators,
  initArchiveEvents
} from './archive.js';
import { initShortcuts } from './shortcuts.js';
import { formatTime, escapeHtml } from './utils.js';

export function parseHash(shows) {
  const raw = window.location.hash.replace(/^#/, '');
  if (!raw) {
    return { showId: shows[0].id, guid: null };
  }

  if (raw.includes('/')) {
    const slashIdx = raw.indexOf('/');
    const sId = raw.slice(0, slashIdx);
    const encGuid = raw.slice(slashIdx + 1);
    let epGuid = null;
    try {
      epGuid = decodeURIComponent(encGuid);
    } catch {
      epGuid = encGuid;
    }
    if (shows.some(s => s.id === sId)) {
      return { showId: sId, guid: epGuid };
    }
  }

  const matched = shows.find(s => s.id === raw);
  if (matched) {
    return { showId: matched.id, guid: null };
  }

  return { showId: 'airshow', guid: raw };
}

export async function loadEpisodeDetail(show, epMeta, seq) {
  const cacheKey = `${show.id}::${epMeta.guid}`;
  state.currentEpisode = {
    ...epMeta,
    highlights: [],
    transcript: [],
    vocab_notes: [],
    references: []
  };
  state.highlightElements = [];
  state.transcriptElements = [];
  resetKnowledgeCards();

  const { highlightsCountBadge, transcriptCountBadge, highlightsGrid, transcriptContainer, searchInput } = elements;

  if (highlightsCountBadge) {
    highlightsCountBadge.textContent = epMeta.highlights_count != null ? `${epMeta.highlights_count} 个片段` : '加载中';
  }
  if (transcriptCountBadge) {
    transcriptCountBadge.textContent = epMeta.transcript_count != null ? `${epMeta.transcript_count} 段落` : '加载中';
  }

  if (highlightsGrid) {
    highlightsGrid.innerHTML = `
      <div class="state-banner state-banner-compact">
        <div class="spinner spinner-sm"></div>
        <p class="state-desc">正在加载精选原声</p>
      </div>
    `;
  }
  if (transcriptContainer) {
    transcriptContainer.innerHTML = `
      <div class="state-banner state-banner-compact">
        <div class="spinner spinner-sm"></div>
        <p class="state-desc">正在加载双语逐字稿</p>
      </div>
    `;
  }

  try {
    const detail = await fetchEpisodeDetail(show, epMeta);

    if (state.currentLoadingGuid !== epMeta.guid || seq !== state.currentRequestSeq || state.currentLoadingShowId !== show.id) {
      return;
    }

    state.currentEpisode = detail;
    renderHighlights(detail.highlights || []);
    renderKnowledgeCards(detail);
    renderTranscript(detail.transcript || []);

    const q = searchInput ? searchInput.value.trim() : '';
    applySearchFilter(q);
  } catch (err) {
    console.error(`[AIRSHOW][UI] Failed to load detail for ${cacheKey}:`, err);
    if (state.currentLoadingGuid !== epMeta.guid || seq !== state.currentRequestSeq || state.currentLoadingShowId !== show.id) return;

    if (highlightsCountBadge) highlightsCountBadge.textContent = '加载失败';
    if (transcriptCountBadge) transcriptCountBadge.textContent = '加载失败';
    if (highlightsGrid) highlightsGrid.innerHTML = '';
    resetKnowledgeCards();
    if (transcriptContainer) {
      transcriptContainer.innerHTML = `
        <div class="state-banner state-banner-compact">
          <h4 class="error-title">单集详情加载失败</h4>
          <p class="state-desc">${escapeHtml(err.message)}</p>
          <button type="button" class="btn-retry" id="btn-retry-detail">重试加载详情</button>
        </div>
      `;
      const btnRetryDetail = document.getElementById('btn-retry-detail');
      if (btnRetryDetail) {
        btnRetryDetail.addEventListener('click', () => {
          selectEpisode(show, epMeta);
        });
      }
    }
  }
}

export function selectEpisode(show, epMeta) {
  throttleSaveProgress(true);
  exitSentenceLoop();
  clearRevealedStates();
  state.currentLoadingGuid = epMeta.guid;
  state.currentRequestSeq++;
  const thisSeq = state.currentRequestSeq;

  console.log(`[AIRSHOW][UI] Selected episode: "${epMeta.title}" (${epMeta.guid}) in ${show.id}`);

  const {
    episodeSelect,
    headerIssueTitle,
    archiveList,
    audio,
    timeTotal,
    timeCurrent,
    seekSlider,
    playerEpTitle,
    playerSpeakerName,
    chapterMarkers,
    playerChapterBadge,
    playerChapterTitle,
    resumeBanner,
    resumeTimeEl,
    searchInput
  } = elements;

  if (episodeSelect) episodeSelect.value = epMeta.guid;
  if (headerIssueTitle) headerIssueTitle.textContent = epMeta.title || '选集目录';

  if (archiveList) {
    const allArchiveItems = archiveList.querySelectorAll('.archive-item');
    allArchiveItems.forEach(item => {
      item.classList.toggle('active', item.dataset.guid === epMeta.guid);
    });
  }

  // Sync URL Hash: #showId/encodeURIComponent(guid)
  const targetHash = `#${show.id}/${encodeURIComponent(epMeta.guid)}`;
  if (window.location.hash !== targetHash) {
    history.replaceState(null, '', targetHash);
  }

  document.title = `${epMeta.title} | ${show.title} · ${(state.currentShowsManifest && state.currentShowsManifest.site_title) || '播客双语精读'}`;

  // Reset audio & player bar
  if (audio) {
    audio.pause();
    audio.src = epMeta.audio_url || '';
    audio.load();
  }
  updateMediaSession(show, epMeta);
  state.lastActiveTranscriptIndex = -1;
  state.lastActiveHighlightIndex = -1;
  if (timeTotal) timeTotal.textContent = formatTime(epMeta.duration_sec || 0);
  if (seekSlider) seekSlider.max = epMeta.duration_sec || 100;
  if (playerEpTitle) {
    playerEpTitle.textContent = epMeta.title || '';
    playerEpTitle.title = epMeta.title || '';
  }
  if (playerSpeakerName) playerSpeakerName.textContent = '未在播放';
  updatePlayPauseIcons(false);

  // Clear chapter indicators
  if (chapterMarkers) chapterMarkers.innerHTML = '';
  if (elements.mastheadChapterTicks) elements.mastheadChapterTicks.innerHTML = '';
  updateMastheadPlayhead(0, epMeta.duration_sec || 0);
  if (playerChapterBadge) playerChapterBadge.style.display = 'none';
  if (playerChapterTitle) playerChapterTitle.textContent = '-';
  if (resumeBanner) resumeBanner.style.display = 'none';
  resetKnowledgeCards();

  // Breakpoint resume restore
  state.userInteractedSeek = false;
  const record = getEpisodeProgress(show.id, epMeta.guid);
  if (record && !record.completed && record.position >= 5) {
    const resumePos = record.position;
    const restorePos = () => {
      if (state.userInteractedSeek || !audio) return;
      try {
        audio.currentTime = resumePos;
        if (seekSlider) seekSlider.value = resumePos;
        if (timeCurrent) timeCurrent.textContent = formatTime(resumePos);
        updateMastheadPlayhead(resumePos, audio.duration || epMeta.duration_sec || 0);
      } catch (err) {
        console.warn('[AIRSHOW][Progress] Error setting resume position:', err);
      }
      if (resumeBanner && !state.userInteractedSeek) {
        if (resumeTimeEl) resumeTimeEl.textContent = formatTime(resumePos);
        resumeBanner.style.display = 'flex';
      }
    };

    if (audio && audio.readyState >= 1) {
      restorePos();
    } else if (audio) {
      audio.addEventListener('loadedmetadata', function onLoadedMeta() {
        audio.removeEventListener('loadedmetadata', onLoadedMeta);
        restorePos();
      }, { once: true });
    }
  } else {
    if (timeCurrent) timeCurrent.textContent = '00:00';
    if (seekSlider) seekSlider.value = 0;
  }

  renderArticleMeta(show, epMeta);
  renderExecutiveSummary(epMeta);

  const cacheKey = `${show.id}::${epMeta.guid}`;
  if (episodeCache.has(cacheKey)) {
    const detail = episodeCache.get(cacheKey);
    state.currentEpisode = detail;
    renderHighlights(detail.highlights || []);
    renderKnowledgeCards(detail);
    renderTranscript(detail.transcript || []);

    const q = searchInput ? searchInput.value.trim() : '';
    applySearchFilter(q);
    return;
  }

  loadEpisodeDetail(show, epMeta, thisSeq);
}

export function handleShowIndexLoaded(show, indexData, targetGuid, seq) {
  if (seq !== state.currentRequestSeq || state.currentLoadingShowId !== show.id) return;

  const episodes = indexData.episodes || [];
  state.currentEpisodesList = episodes;

  const {
    loadingState,
    errorState,
    emptyState,
    episodeContent,
    playerBar,
    emptyShowTitle,
    emptyShowDesc,
    emptyRssLink,
    headerIssueTitle,
    headerIssueCount,
    btnOpenArchive,
    mobileArchiveBtn,
    searchInput
  } = elements;

  if (episodes.length === 0) {
    if (loadingState) loadingState.style.display = 'none';
    if (errorState) errorState.style.display = 'none';
    if (episodeContent) episodeContent.style.display = 'none';
    if (playerBar) playerBar.style.display = 'none';
    if (emptyState) emptyState.style.display = 'flex';
    resetKnowledgeCards();

    if (emptyShowTitle) emptyShowTitle.textContent = `${show.title} · 暂无收录单集`;
    if (emptyShowDesc) {
      emptyShowDesc.textContent = `该播客暂未收录已转写的精读单集，最新一期整理完成后会自动出现在这里。`;
    }
    if (emptyRssLink) emptyRssLink.textContent = `RSS: ${show.rss_url || indexData.rss_url || '-'}`;

    if (headerIssueTitle) headerIssueTitle.textContent = '暂无单集';
    if (headerIssueCount) headerIssueCount.textContent = '0 期';
    if (btnOpenArchive) btnOpenArchive.disabled = true;
    if (mobileArchiveBtn) mobileArchiveBtn.disabled = true;
    if (searchInput) searchInput.disabled = true;

    if (window.location.hash !== '#' + show.id) {
      history.replaceState(null, '', '#' + show.id);
    }
    return;
  }

  // Active State with Episodes
  if (emptyState) emptyState.style.display = 'none';
  if (loadingState) loadingState.style.display = 'none';
  if (btnOpenArchive) btnOpenArchive.disabled = false;
  if (mobileArchiveBtn) mobileArchiveBtn.disabled = false;
  if (searchInput) searchInput.disabled = false;

  renderEpisodeArchive(show, episodes, (s, ep) => selectEpisode(s, ep));

  let initialEp = null;
  if (targetGuid) {
    initialEp = episodes.find(e => e.guid === targetGuid);
  }
  if (!initialEp) {
    initialEp = episodes[0];
  }

  selectEpisode(show, initialEp);

  if (episodeContent) episodeContent.style.display = 'block';
  if (playerBar) playerBar.style.display = 'block';
}

export async function switchShow(showId, targetGuid = null) {
  if (!state.currentShowsManifest || !state.currentShowsManifest.shows) return;
  const show = state.currentShowsManifest.shows.find(s => s.id === showId);
  if (!show) {
    console.warn(`[AIRSHOW][UI] Show ${showId} not found in manifest`);
    return;
  }

  state.currentRequestSeq++;
  const thisSeq = state.currentRequestSeq;
  state.currentLoadingShowId = showId;
  state.currentShow = show;

  applyShowTheme(show);
  updatePodcastTabsActive(showId);

  // Pause player and reset playback state
  throttleSaveProgress(true);
  exitSentenceLoop();
  clearRevealedStates();

  const {
    audio,
    timeCurrent,
    timeTotal,
    seekSlider,
    playerEpTitle,
    playerSpeakerName,
    chapterMarkers,
    playerChapterBadge,
    playerChapterTitle,
    resumeBanner,
    searchInput,
    searchClearBtn,
    searchStatsBanner,
    brandShowTitle,
    loadingState,
    errorState,
    emptyState,
    episodeContent,
    playerBar,
    errorMessage
  } = elements;

  if (audio) {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
  }
  state.lastActiveTranscriptIndex = -1;
  state.lastActiveHighlightIndex = -1;
  if (timeCurrent) timeCurrent.textContent = '00:00';
  if (timeTotal) timeTotal.textContent = '00:00';
  if (seekSlider) seekSlider.value = 0;
  if (playerEpTitle) playerEpTitle.textContent = show.title;
  if (playerSpeakerName) playerSpeakerName.textContent = '未在播放';
  updatePlayPauseIcons(false);

  // Clear chapter and resume indicators
  if (chapterMarkers) chapterMarkers.innerHTML = '';
  if (elements.mastheadChapterTicks) elements.mastheadChapterTicks.innerHTML = '';
  updateMastheadPlayhead(0, 0);
  if (playerChapterBadge) playerChapterBadge.style.display = 'none';
  if (playerChapterTitle) playerChapterTitle.textContent = '-';
  if (resumeBanner) resumeBanner.style.display = 'none';
  resetKnowledgeCards();

  // Reset search filter
  if (searchInput) searchInput.value = '';
  if (searchClearBtn) searchClearBtn.style.display = 'none';
  if (searchStatsBanner) searchStatsBanner.classList.remove('visible');

  // Brand & title sync
  if (brandShowTitle) brandShowTitle.textContent = state.currentShowsManifest.site_title || '播客双语精读';
  document.title = `${show.title} · ${state.currentShowsManifest.site_title || '播客双语精读'}`;

  // Cache hit branch
  if (showIndexCache.has(showId)) {
    const indexData = showIndexCache.get(showId);
    handleShowIndexLoaded(show, indexData, targetGuid, thisSeq);
    return;
  }

  // Cache miss
  if (loadingState) loadingState.style.display = 'flex';
  if (errorState) errorState.style.display = 'none';
  if (emptyState) emptyState.style.display = 'none';
  if (episodeContent) episodeContent.style.display = 'none';
  if (playerBar) playerBar.style.display = 'none';

  try {
    const indexData = await fetchShowIndex(show);

    if (thisSeq !== state.currentRequestSeq || state.currentLoadingShowId !== showId) {
      return;
    }

    handleShowIndexLoaded(show, indexData, targetGuid, thisSeq);
  } catch (err) {
    console.error(`[AIRSHOW][UI] Failed to load index for show ${showId}:`, err);
    if (thisSeq !== state.currentRequestSeq || state.currentLoadingShowId !== showId) return;

    if (loadingState) loadingState.style.display = 'none';
    if (errorState) errorState.style.display = 'flex';
    if (errorMessage) errorMessage.textContent = `加载节目目录失败：${err.message}`;
  }
}

export async function initApp() {
  console.log('[AIRSHOW][UI] Initializing application...');
  exitSentenceLoop();
  clearRevealedStates();

  const { loadingState, errorState, emptyState, episodeContent, playerBar, errorMessage } = elements;
  if (loadingState) loadingState.style.display = 'flex';
  if (errorState) errorState.style.display = 'none';
  if (emptyState) emptyState.style.display = 'none';
  if (episodeContent) episodeContent.style.display = 'none';
  if (playerBar) playerBar.style.display = 'none';

  try {
    const showsData = await fetchShows();
    state.currentShowsManifest = showsData;

    if (!showsData.shows || showsData.shows.length === 0) {
      throw new Error('shows.json 中未配置任何播客节目');
    }

    renderPodcastTabs(showsData.shows, (showId) => switchShow(showId));

    const { showId, guid } = parseHash(showsData.shows);
    await switchShow(showId, guid);
  } catch (err) {
    console.error('[AIRSHOW][UI] App initialization failed:', err);
    if (loadingState) loadingState.style.display = 'none';
    if (errorState) errorState.style.display = 'flex';
    if (errorMessage) {
      errorMessage.textContent = `初始化失败：${err.message}。请确认已在 docs 目录下运行静态服务器。`;
    }
  }
}

export function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  const isLocalhost = Boolean(
    window.location.hostname === 'localhost' ||
    window.location.hostname === '127.0.0.1' ||
    window.location.hostname === '[::1]'
  );
  if (window.location.protocol === 'https:' || isLocalhost) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('./sw.js')
        .then(reg => {
          console.log('[AIRSHOW][UI] ServiceWorker registered with scope:', reg.scope);
        })
        .catch(err => {
          console.warn('[AIRSHOW][UI] ServiceWorker registration failed:', err);
        });
    });
  }
}

// Global bootstrap
function bootstrap() {
  initElements();
  setProgressUpdateCallback(updateEpisodeProgressIndicators);

  detectPwaMode();
  initTheme();
  initLanguageMode();
  initBlurMode();
  initMediaSessionActionHandlers();
  initPlayerEvents();
  initReaderEvents();
  initArchiveEvents();
  initShortcuts();

  // Top-level DOM Events
  if (elements.episodeSelect) {
    elements.episodeSelect.addEventListener('change', (e) => {
      const selectedGuid = e.target.value;
      if (!state.currentShow) return;
      const indexData = showIndexCache.get(state.currentShow.id);
      if (!indexData || !indexData.episodes) return;
      const found = indexData.episodes.find(ep => ep.guid === selectedGuid);
      if (found) {
        selectEpisode(state.currentShow, found);
      }
    });
  }

  if (elements.themeToggleBtn) {
    elements.themeToggleBtn.addEventListener('click', toggleTheme);
  }

  if (elements.brandLink) {
    elements.brandLink.addEventListener('click', (e) => {
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  if (elements.btnRetry) {
    elements.btnRetry.addEventListener('click', initApp);
  }

  window.addEventListener('hashchange', () => {
    if (!state.currentShowsManifest || !state.currentShowsManifest.shows) return;
    const { showId, guid } = parseHash(state.currentShowsManifest.shows);
    if (state.currentShow && state.currentShow.id === showId) {
      if (guid && state.currentEpisode && state.currentEpisode.guid === guid) {
        return;
      }
      const indexData = showIndexCache.get(showId);
      if (indexData && indexData.episodes) {
        const found = indexData.episodes.find(ep => ep.guid === guid);
        if (found) {
          selectEpisode(state.currentShow, found);
          return;
        }
      }
    }
    switchShow(showId, guid);
  });

  registerServiceWorker();
  initApp();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap);
} else {
  bootstrap();
}
