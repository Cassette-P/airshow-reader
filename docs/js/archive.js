/**
 * archive.js - Podcast show tabs, overflow mask,
 *              Issue Archive drawer, gestures, and episode progress sync
 */

import { state, elements } from './state.js';
import { getEpisodeProgress } from './storage.js';
import { formatDateZh, formatDurationZh } from './utils.js';

let archiveCloseTimer = null;

const PROGRESS_ICON_CHECK = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
const PROGRESS_ICON_PLAY = `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"></polygon></svg>`;

export function updateTabsOverflowMask() {
  const { podcastTabs } = elements;
  if (!podcastTabs) return;
  const isOverflow = (podcastTabs.scrollWidth - podcastTabs.scrollLeft - podcastTabs.clientWidth) > 2;
  podcastTabs.classList.toggle('is-overflow-right', isOverflow);
}

export function renderPodcastTabs(shows, onSelectShow) {
  const { podcastTabs } = elements;
  if (!podcastTabs) return;
  podcastTabs.innerHTML = '';

  shows.forEach((show) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'show-tab-btn';
    btn.dataset.showId = show.id;
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', 'false');
    btn.setAttribute('title', `${show.title} · ${show.subtitle || ''}`);

    const titleSpan = document.createElement('span');
    titleSpan.textContent = show.title;

    const countSpan = document.createElement('span');
    countSpan.className = 'show-tab-count tnum';
    countSpan.textContent = `${show.episode_count != null ? show.episode_count : '0'}`;

    btn.appendChild(titleSpan);
    btn.appendChild(countSpan);

    btn.addEventListener('click', () => {
      if (state.currentShow && state.currentShow.id === show.id) return;
      if (onSelectShow) onSelectShow(show.id);
    });

    podcastTabs.appendChild(btn);
  });
  updateTabsOverflowMask();
}

export function updatePodcastTabsActive(showId) {
  const { podcastTabs } = elements;
  if (!podcastTabs) return;
  const tabs = podcastTabs.querySelectorAll('.show-tab-btn');
  tabs.forEach((tab) => {
    const isActive = tab.dataset.showId === showId;
    tab.classList.toggle('active', isActive);
    tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
    if (isActive) {
      tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  });
  updateTabsOverflowMask();
}

export function formatOptionText(ep, record) {
  let tag = '';
  if (record) {
    if (record.completed) {
      tag = ' [✓ 已听完]';
    } else if (record.position >= 5) {
      const dur = record.duration || ep.duration_sec || 0;
      if (dur > 0) {
        const pct = Math.min(99, Math.round((record.position / dur) * 100));
        tag = ` [▶ ${pct}%]`;
      }
    }
  }
  return `${formatDateZh(ep.pub_date)} | ${ep.title}${tag}`;
}

export function renderEpisodeArchive(show, episodes, onSelectEpisode) {
  const { archiveList, headerIssueCount, archiveShowTitle, archiveShowTag, mobileArchiveBtn, episodeSelect } = elements;
  if (!archiveList) return;

  archiveList.innerHTML = '';
  if (headerIssueCount) headerIssueCount.textContent = `${episodes.length} 期`;
  if (archiveShowTitle) archiveShowTitle.textContent = `${show.title} · 往期选集`;
  if (archiveShowTag) archiveShowTag.textContent = show.subtitle || '往期选集';
  if (mobileArchiveBtn) {
    mobileArchiveBtn.setAttribute('aria-label', `往期选集目录 (${episodes.length} 期)`);
  }

  // Fallback select sync
  if (episodeSelect) {
    episodeSelect.innerHTML = '';
  }

  episodes.forEach((ep, idx) => {
    const record = getEpisodeProgress(show.id, ep.guid);

    // Fallback select option
    if (episodeSelect) {
      const opt = document.createElement('option');
      opt.value = ep.guid;
      opt.textContent = formatOptionText(ep, record);
      episodeSelect.appendChild(opt);
    }

    // Archive Drawer Item
    const card = document.createElement('div');
    card.className = 'archive-item';
    card.dataset.guid = ep.guid;
    if (state.currentEpisode && state.currentEpisode.guid === ep.guid) {
      card.classList.add('active');
    }

    const topRow = document.createElement('div');
    topRow.className = 'archive-item-top';

    const issueBadge = document.createElement('span');
    issueBadge.className = 'archive-issue-badge tnum';
    issueBadge.textContent = `#${episodes.length - idx}`;

    const metaSpan = document.createElement('span');
    metaSpan.className = 'archive-item-meta tnum';
    metaSpan.textContent = `${formatDateZh(ep.pub_date)} · ${formatDurationZh(ep.duration_sec)} · ${ep.transcript_count || 0} 段`;

    topRow.appendChild(issueBadge);
    topRow.appendChild(metaSpan);

    if (record && record.completed) {
      const progressBadge = document.createElement('span');
      progressBadge.className = 'archive-progress-tag completed';
      progressBadge.innerHTML = `${PROGRESS_ICON_CHECK}已听完`;
      topRow.appendChild(progressBadge);
    } else if (record && record.position >= 5) {
      const dur = record.duration || ep.duration_sec || 0;
      const pct = dur > 0 ? Math.min(99, Math.round((record.position / dur) * 100)) : 0;
      const progressBadge = document.createElement('span');
      progressBadge.className = 'archive-progress-tag in-progress';
      progressBadge.innerHTML = `${PROGRESS_ICON_PLAY}${pct}%`;
      topRow.appendChild(progressBadge);
    }

    const titleEl = document.createElement('h4');
    titleEl.className = 'archive-item-title';
    titleEl.textContent = ep.title;

    const descEl = document.createElement('p');
    descEl.className = 'archive-item-desc';
    descEl.textContent = ep.summary_zh || '';

    card.appendChild(topRow);
    card.appendChild(titleEl);
    card.appendChild(descEl);

    card.addEventListener('click', () => {
      if (onSelectEpisode) onSelectEpisode(show, ep);
      closeArchiveDrawer();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    archiveList.appendChild(card);
  });
}

export function updateEpisodeProgressIndicators(showId, guid) {
  if (!showId || !guid) return;
  const record = getEpisodeProgress(showId, guid);
  const { episodeSelect, archiveList } = elements;

  // 1. Fallback select option sync
  if (episodeSelect) {
    const options = episodeSelect.querySelectorAll('option');
    for (const opt of options) {
      if (opt.value === guid) {
        const ep = state.currentEpisodesList.find(e => e.guid === guid);
        if (ep) {
          opt.textContent = formatOptionText(ep, record);
        }
        break;
      }
    }
  }

  // 2. Archive Drawer card sync
  if (archiveList) {
    const card = archiveList.querySelector(`.archive-item[data-guid="${CSS.escape(guid)}"]`);
    if (card) {
      let progressBadge = card.querySelector('.archive-progress-tag');
      if (!progressBadge) {
        progressBadge = document.createElement('span');
        progressBadge.className = 'archive-progress-tag';
        const topRow = card.querySelector('.archive-item-top');
        if (topRow) topRow.appendChild(progressBadge);
      }
      if (record && record.completed) {
        progressBadge.className = 'archive-progress-tag completed';
        progressBadge.innerHTML = `${PROGRESS_ICON_CHECK}已听完`;
        progressBadge.style.display = 'inline-flex';
      } else if (record && record.position >= 5) {
        const ep = state.currentEpisodesList.find(e => e.guid === guid);
        const dur = record.duration || (ep && ep.duration_sec) || 0;
        const pct = dur > 0 ? Math.min(99, Math.round((record.position / dur) * 100)) : 0;
        progressBadge.className = 'archive-progress-tag in-progress';
        progressBadge.innerHTML = `${PROGRESS_ICON_PLAY}${pct}%`;
        progressBadge.style.display = 'inline-flex';
      } else {
        progressBadge.style.display = 'none';
      }
    }
  }
}

export function openArchiveDrawer() {
  const { archiveDrawer, archiveBackdrop, btnOpenArchive, mobileArchiveBtn } = elements;
  if (!archiveDrawer || !archiveBackdrop) return;

  if (archiveCloseTimer) {
    clearTimeout(archiveCloseTimer);
    archiveCloseTimer = null;
  }
  archiveDrawer.style.transform = '';
  archiveBackdrop.style.display = 'block';
  archiveDrawer.style.display = 'flex';
  void archiveDrawer.offsetWidth;
  requestAnimationFrame(() => {
    archiveBackdrop.classList.add('visible');
    archiveDrawer.classList.add('open');
  });
  if (btnOpenArchive) btnOpenArchive.setAttribute('aria-expanded', 'true');
  if (mobileArchiveBtn) mobileArchiveBtn.setAttribute('aria-expanded', 'true');
  document.body.style.overflow = 'hidden';
}

export function closeArchiveDrawer() {
  const { archiveDrawer, archiveBackdrop, btnOpenArchive, mobileArchiveBtn } = elements;
  if (!archiveDrawer || !archiveBackdrop) return;

  archiveDrawer.style.transform = '';
  archiveBackdrop.classList.remove('visible');
  archiveDrawer.classList.remove('open');
  if (btnOpenArchive) btnOpenArchive.setAttribute('aria-expanded', 'false');
  if (mobileArchiveBtn) mobileArchiveBtn.setAttribute('aria-expanded', 'false');
  document.body.style.overflow = '';
  if (archiveCloseTimer) {
    clearTimeout(archiveCloseTimer);
  }
  archiveCloseTimer = setTimeout(() => {
    archiveBackdrop.style.display = 'none';
    archiveDrawer.style.display = 'none';
    archiveCloseTimer = null;
  }, 280);
}

export function toggleArchiveDrawer() {
  const { archiveDrawer } = elements;
  if (archiveDrawer && archiveDrawer.classList.contains('open')) {
    closeArchiveDrawer();
  } else {
    openArchiveDrawer();
  }
}

export function initArchiveEvents() {
  const { podcastTabs, btnOpenArchive, mobileArchiveBtn, archiveCloseBtn, archiveBackdrop, archiveGrabber, archiveHeader } = elements;

  if (podcastTabs) {
    podcastTabs.addEventListener('scroll', updateTabsOverflowMask, { passive: true });
  }
  window.addEventListener('resize', updateTabsOverflowMask);

  if (btnOpenArchive) {
    btnOpenArchive.addEventListener('click', toggleArchiveDrawer);
  }
  if (mobileArchiveBtn) {
    mobileArchiveBtn.addEventListener('click', toggleArchiveDrawer);
  }
  if (archiveCloseBtn) {
    archiveCloseBtn.addEventListener('click', closeArchiveDrawer);
  }
  if (archiveBackdrop) {
    archiveBackdrop.addEventListener('click', closeArchiveDrawer);
  }

  // Mobile Bottom-sheet Drag-to-close Gestures
  let drawerTouchStartY = 0;
  let drawerTouchDeltaY = 0;
  let isDraggingDrawer = false;

  function onDrawerTouchStart(e) {
    if (e.touches && e.touches.length > 0) {
      drawerTouchStartY = e.touches[0].clientY;
      drawerTouchDeltaY = 0;
      isDraggingDrawer = true;
    }
  }

  function onDrawerTouchMove(e) {
    if (!isDraggingDrawer || !e.touches || e.touches.length === 0) return;
    const currentY = e.touches[0].clientY;
    drawerTouchDeltaY = currentY - drawerTouchStartY;
    if (drawerTouchDeltaY > 0 && window.matchMedia('(max-width: 640px)').matches && elements.archiveDrawer) {
      elements.archiveDrawer.style.transform = `translateY(${drawerTouchDeltaY}px)`;
    }
  }

  function onDrawerTouchEnd() {
    if (!isDraggingDrawer) return;
    isDraggingDrawer = false;
    if (elements.archiveDrawer) {
      elements.archiveDrawer.style.transform = '';
    }
    if (drawerTouchDeltaY > 60 && window.matchMedia('(max-width: 640px)').matches) {
      closeArchiveDrawer();
    }
    drawerTouchStartY = 0;
    drawerTouchDeltaY = 0;
  }

  if (archiveGrabber) {
    archiveGrabber.addEventListener('touchstart', onDrawerTouchStart, { passive: true });
    archiveGrabber.addEventListener('touchmove', onDrawerTouchMove, { passive: true });
    archiveGrabber.addEventListener('touchend', onDrawerTouchEnd, { passive: true });
    archiveGrabber.addEventListener('touchcancel', onDrawerTouchEnd, { passive: true });
  }
  if (archiveHeader) {
    archiveHeader.addEventListener('touchstart', onDrawerTouchStart, { passive: true });
    archiveHeader.addEventListener('touchmove', onDrawerTouchMove, { passive: true });
    archiveHeader.addEventListener('touchend', onDrawerTouchEnd, { passive: true });
    archiveHeader.addEventListener('touchcancel', onDrawerTouchEnd, { passive: true });
  }
}
