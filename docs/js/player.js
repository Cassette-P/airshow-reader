/**
 * player.js - Audio playback engine, seekAndPlay, chapter markers,
 *             sentence loop, progress memory, and Media Session integration
 */

import { state, elements, PLAYBACK_RATES } from './state.js';
import { saveEpisodeProgress } from './storage.js';
import { formatTime, binarySearchActiveIndex } from './utils.js';

let updateEpisodeProgressCallback = null;

export function setProgressUpdateCallback(cb) {
  updateEpisodeProgressCallback = cb;
}

export function updateMediaSession(show, epMeta) {
  if (!('mediaSession' in navigator) || !show || !epMeta) return;
  try {
    state.currentActiveShow = show;
    state.currentActiveEpisodeMeta = epMeta;

    const icon512Url = new URL('./icons/icon-512.png', window.location.href).href;
    const icon192Url = new URL('./icons/icon-192.png', window.location.href).href;
    const showArtworkUrl = (show.id === 'airshow')
      ? new URL('./artwork-airshow-512.png', window.location.href).href
      : (show.id === 'sinica')
        ? new URL('./artwork-sinica-512.png', window.location.href).href
        : (show.id === 'tal')
          ? new URL('./artwork-tal-512.png', window.location.href).href
          : icon512Url;

    navigator.mediaSession.metadata = new MediaMetadata({
      title: epMeta.title || '',
      artist: show.title || 'Podcast Bilingual Reader',
      album: show.subtitle || (state.currentShowsManifest && state.currentShowsManifest.site_title) || '播客双语精读',
      artwork: [
        { src: showArtworkUrl, sizes: '512x512', type: 'image/png' },
        { src: icon192Url, sizes: '192x192', type: 'image/png' }
      ]
    });
  } catch (err) {
    console.warn('[AIRSHOW][MediaSession] Failed to update mediaSession metadata:', err);
  }
}

export function updateMediaPositionState() {
  if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState || !elements.audio) return;
  try {
    const duration = elements.audio.duration;
    const position = elements.audio.currentTime;
    if (
      Number.isFinite(duration) &&
      duration > 0 &&
      Number.isFinite(position) &&
      position >= 0 &&
      position <= duration
    ) {
      navigator.mediaSession.setPositionState({
        duration: duration,
        playbackRate: elements.audio.playbackRate || 1,
        position: position
      });
    }
  } catch (e) {
    console.warn('[AIRSHOW][MediaSession] Failed to setPositionState:', e);
  }
}

export function initMediaSessionActionHandlers() {
  if (!('mediaSession' in navigator)) return;

  const handlers = {
    play: () => {
      if (elements.audio && elements.audio.paused && elements.audio.src) {
        elements.audio.play().catch(err => console.warn('[AIRSHOW][MediaSession] Play failed:', err));
      }
    },
    pause: () => {
      if (elements.audio && !elements.audio.paused) {
        elements.audio.pause();
      }
    },
    seekbackward: (details) => {
      const skip = (details && details.seekOffset) || 10;
      if (elements.audio) {
        seekAndPlay(Math.max(0, elements.audio.currentTime - skip));
      }
    },
    seekforward: (details) => {
      const skip = (details && details.seekOffset) || 10;
      if (elements.audio) {
        const maxSec = elements.audio.duration || (state.currentActiveEpisodeMeta ? state.currentActiveEpisodeMeta.duration_sec : Infinity);
        seekAndPlay(Math.min(maxSec, elements.audio.currentTime + skip));
      }
    },
    seekto: (details) => {
      if (details && details.seekTime !== undefined && isFinite(details.seekTime)) {
        seekAndPlay(details.seekTime);
      }
    }
  };

  for (const [action, handler] of Object.entries(handlers)) {
    try {
      navigator.mediaSession.setActionHandler(action, handler);
    } catch (e) {
      console.warn(`[AIRSHOW][MediaSession] Failed to register handler for ${action}:`, e);
    }
  }
}

export function seekAndPlay(targetSec) {
  if (!elements.audio) return;
  targetSec = Math.max(0, Number(targetSec) || 0);
  state.userInteractedSeek = true;
  if (elements.resumeBanner) elements.resumeBanner.style.display = 'none';

  // M8-A: If seeking outside of active sentence loop range, exit loop
  if (state.loopState.active) {
    if (targetSec < state.loopState.startSec - 0.5 || targetSec > state.loopState.endSec + 0.5) {
      exitSentenceLoop();
    }
  }

  console.log(`[AIRSHOW][UI] Seeking to ${targetSec}s (readyState: ${elements.audio.readyState})`);

  const executeSeekAndPlay = () => {
    try {
      elements.audio.currentTime = targetSec;
    } catch (err) {
      console.warn('[AIRSHOW][UI] Setting currentTime failed:', err);
    }
    const playPromise = elements.audio.play();
    if (playPromise !== undefined) {
      playPromise.catch(err => {
        console.warn('[AIRSHOW][UI] Play call rejected/interrupted:', err);
      });
    }
    updateMediaPositionState();
  };

  if (elements.audio.readyState < 1) {
    elements.audio.addEventListener('loadedmetadata', function onLoaded() {
      elements.audio.removeEventListener('loadedmetadata', onLoaded);
      executeSeekAndPlay();
    }, { once: true });

    if (elements.audio.networkState === HTMLMediaElement.NETWORK_EMPTY) {
      elements.audio.load();
    }
  } else {
    executeSeekAndPlay();
  }
}

export function startSentenceLoop(idx) {
  if (!state.currentEpisode || !state.currentEpisode.transcript || !state.currentEpisode.transcript[idx]) return;
  const tr = state.currentEpisode.transcript;
  const item = tr[idx];
  const startSec = item.start_sec;
  let endSec = (idx + 1 < tr.length) ? tr[idx + 1].start_sec : (elements.audio.duration || state.currentEpisode.duration_sec || (startSec + 3));
  if (!Number.isFinite(endSec) || (endSec - startSec < 1.5)) {
    endSec = startSec + 3;
  }

  // Clear previous loop classes
  state.transcriptElements.forEach((el) => {
    el.classList.remove('is-looping');
    const b = el.querySelector('.btn-sentence-loop');
    if (b) b.classList.remove('is-looping');
  });

  state.loopState = {
    active: true,
    index: idx,
    startSec: startSec,
    endSec: endSec
  };

  if (state.transcriptElements[idx]) {
    state.transcriptElements[idx].classList.add('is-looping');
    const curBtn = state.transcriptElements[idx].querySelector('.btn-sentence-loop');
    if (curBtn) curBtn.classList.add('is-looping');
  }

  if (elements.loopBanner && elements.loopBannerText) {
    const idxStr = '#' + String(idx + 1);
    const rangeStr = `${formatTime(startSec)}–${formatTime(endSec)}`;
    elements.loopBannerText.textContent = `复读中 ${idxStr} · ${rangeStr}`;
    elements.loopBanner.style.display = 'flex';
  }

  seekAndPlay(startSec);
}

export function exitSentenceLoop() {
  if (!state.loopState.active) return;
  state.loopState = {
    active: false,
    index: -1,
    startSec: 0,
    endSec: 0
  };
  state.transcriptElements.forEach((el) => {
    el.classList.remove('is-looping');
    const b = el.querySelector('.btn-sentence-loop');
    if (b) b.classList.remove('is-looping');
  });
  if (elements.loopBanner) {
    elements.loopBanner.style.display = 'none';
  }
}

export function toggleSentenceLoop(idx) {
  if (state.loopState.active && state.loopState.index === idx) {
    exitSentenceLoop();
  } else {
    startSentenceLoop(idx);
  }
}

export function updateMastheadPlayhead(currentTime, duration) {
  const { mastheadRulerProgress, mastheadPlayhead } = elements;
  const dur = Number(duration) || 0;
  const cur = Number(currentTime) || 0;
  const ratio = dur > 0 ? Math.min(1, Math.max(0, cur / dur)) : 0;

  if (mastheadRulerProgress) {
    mastheadRulerProgress.style.transform = `translateY(-50%) scaleX(${ratio})`;
  }
  if (mastheadPlayhead) {
    mastheadPlayhead.style.left = `${ratio * 100}%`;
    mastheadPlayhead.classList.toggle('visible', ratio > 0);
  }
}

export function showMastheadRulerTooltip(pct, hl) {
  const { mastheadRulerTooltip } = elements;
  if (!mastheadRulerTooltip) return;
  mastheadRulerTooltip.textContent = `${formatTime(hl.start_sec)} · ${hl.title_zh || ''}`;
  mastheadRulerTooltip.style.left = `${Math.min(92, Math.max(8, pct))}%`;
  mastheadRulerTooltip.style.display = 'block';
  requestAnimationFrame(() => {
    if (mastheadRulerTooltip) mastheadRulerTooltip.style.opacity = '1';
  });
}

export function hideMastheadRulerTooltip() {
  const { mastheadRulerTooltip } = elements;
  if (!mastheadRulerTooltip) return;
  mastheadRulerTooltip.style.opacity = '0';
  mastheadRulerTooltip.style.display = 'none';
}

export function renderChapterMarkers(highlights) {
  if (elements.chapterMarkers) elements.chapterMarkers.innerHTML = '';
  if (elements.mastheadChapterTicks) elements.mastheadChapterTicks.innerHTML = '';
  if (!highlights || highlights.length === 0) return;

  const duration = (state.currentEpisode && state.currentEpisode.duration_sec) ||
    (state.currentActiveEpisodeMeta && state.currentActiveEpisodeMeta.duration_sec) ||
    (elements.audio && elements.audio.duration) || 0;
  if (!duration || duration <= 0) return;

  highlights.forEach((hl, idx) => {
    if (hl.start_sec == null || hl.start_sec < 0) return;
    const pct = Math.min(100, Math.max(0, (hl.start_sec / duration) * 100));

    // 1. Floating player scrubber marker
    if (elements.chapterMarkers) {
      const marker = document.createElement('div');
      marker.className = 'chapter-marker';
      marker.style.left = `${pct}%`;
      marker.dataset.startSec = hl.start_sec;

      marker.addEventListener('mouseenter', () => {
        showMarkerTooltip(pct, hl);
      });
      marker.addEventListener('mouseleave', () => {
        hideMarkerTooltip();
      });
      marker.addEventListener('click', (e) => {
        e.stopPropagation();
        state.userInteractedSeek = true;
        if (elements.resumeBanner) elements.resumeBanner.style.display = 'none';
        seekAndPlay(hl.start_sec);
      });

      elements.chapterMarkers.appendChild(marker);
    }

    // 2. Live Masthead acoustic ruler chapter anchor tick
    if (elements.mastheadChapterTicks) {
      const tick = document.createElement('button');
      tick.type = 'button';
      tick.className = 'masthead-chapter-tick';
      tick.style.left = `${pct}%`;
      tick.dataset.index = idx;
      tick.dataset.startSec = hl.start_sec;
      tick.setAttribute('aria-label', `跳转至 ${formatTime(hl.start_sec)} ${hl.title_zh || ''}`);

      tick.addEventListener('mouseenter', () => showMastheadRulerTooltip(pct, hl));
      tick.addEventListener('mouseleave', hideMastheadRulerTooltip);
      tick.addEventListener('focus', () => showMastheadRulerTooltip(pct, hl));
      tick.addEventListener('blur', hideMastheadRulerTooltip);
      tick.addEventListener('click', (e) => {
        e.stopPropagation();
        if (state.loopState.active) {
          exitSentenceLoop();
        }
        state.userInteractedSeek = true;
        if (elements.resumeBanner) elements.resumeBanner.style.display = 'none';
        seekAndPlay(hl.start_sec);
      });

      elements.mastheadChapterTicks.appendChild(tick);
    }
  });
}

export function showMarkerTooltip(pct, hl) {
  if (!elements.markerTooltip) return;
  elements.markerTooltip.textContent = `${formatTime(hl.start_sec)} · ${hl.title_zh || ''}`;
  elements.markerTooltip.style.left = `${pct}%`;
  elements.markerTooltip.style.display = 'block';
  requestAnimationFrame(() => {
    if (elements.markerTooltip) elements.markerTooltip.style.opacity = '1';
  });
}

export function hideMarkerTooltip() {
  if (!elements.markerTooltip) return;
  elements.markerTooltip.style.opacity = '0';
  elements.markerTooltip.style.display = 'none';
}

export function throttleSaveProgress(force = false) {
  if (!state.currentShow || !state.currentActiveEpisodeMeta || !elements.audio) return;
  const now = Date.now();
  if (!force && (now - state.lastProgressWriteTime < 3000)) {
    return;
  }
  state.lastProgressWriteTime = now;
  const pos = elements.audio.currentTime;
  const dur = (Number.isFinite(elements.audio.duration) && elements.audio.duration > 0)
    ? elements.audio.duration
    : (state.currentActiveEpisodeMeta.duration_sec || 0);

  saveEpisodeProgress(state.currentShow.id, state.currentActiveEpisodeMeta.guid, pos, dur);
  if (updateEpisodeProgressCallback) {
    updateEpisodeProgressCallback(state.currentShow.id, state.currentActiveEpisodeMeta.guid);
  }
}

export function updatePlayPauseIcons(isPlaying) {
  if (!elements.btnPlayPause) return;
  if (isPlaying) {
    if (elements.iconPlay) elements.iconPlay.style.display = 'none';
    if (elements.iconPause) elements.iconPause.style.display = 'block';
    elements.btnPlayPause.setAttribute('aria-label', '暂停');
  } else {
    if (elements.iconPlay) elements.iconPlay.style.display = 'block';
    if (elements.iconPause) elements.iconPause.style.display = 'none';
    elements.btnPlayPause.setAttribute('aria-label', '播放');
  }
}

export function togglePlayPause() {
  if (!elements.audio || !elements.audio.src) return;
  if (elements.audio.paused) {
    elements.audio.play().catch(err => console.warn('[AIRSHOW][UI] Play rejected:', err));
  } else {
    elements.audio.pause();
  }
}

export function rewind(seconds = 15) {
  if (!elements.audio) return;
  const target = Math.max(0, elements.audio.currentTime - seconds);
  seekAndPlay(target);
}

export function fastForward(seconds = 15) {
  if (!elements.audio) return;
  const maxSec = elements.audio.duration || (state.currentEpisode ? state.currentEpisode.duration_sec : Infinity);
  const target = Math.min(maxSec, elements.audio.currentTime + seconds);
  seekAndPlay(target);
}

export function cyclePlaybackRate() {
  if (!elements.audio || !elements.btnSpeed) return;
  state.currentRateIndex = (state.currentRateIndex + 1) % PLAYBACK_RATES.length;
  const newRate = PLAYBACK_RATES[state.currentRateIndex];
  elements.audio.playbackRate = newRate;
  const rateText = Number.isInteger(newRate) ? `${newRate.toFixed(1)}x` : `${newRate}x`;
  elements.btnSpeed.textContent = rateText;
  elements.btnSpeed.setAttribute('aria-label', `播放倍速 ${rateText}`);
  updateMediaPositionState();
}

export function toggleAutoScroll() {
  if (!elements.btnAutoscroll) return;
  state.isAutoScrollEnabled = !state.isAutoScrollEnabled;
  elements.btnAutoscroll.classList.toggle('active', state.isAutoScrollEnabled);
  if (state.isAutoScrollEnabled && state.lastActiveTranscriptIndex !== -1 && state.transcriptElements[state.lastActiveTranscriptIndex]) {
    state.transcriptElements[state.lastActiveTranscriptIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

export function initPlayerEvents() {
  const { audio, seekSlider, timeCurrent, timeTotal, btnPlayPause, btnRewind, btnForward, btnSpeed, btnAutoscroll, playerChapterBadge, playerChapterTitle, playerSpeakerName, resumeBanner, btnResumePlay, btnResumeRestart, btnResumeClose, btnLoopExit } = elements;

  if (!audio) return;

  btnPlayPause.addEventListener('click', togglePlayPause);
  btnRewind.addEventListener('click', () => rewind(15));
  btnForward.addEventListener('click', () => fastForward(15));
  btnSpeed.addEventListener('click', cyclePlaybackRate);
  btnAutoscroll.addEventListener('click', toggleAutoScroll);

  if (btnLoopExit) {
    btnLoopExit.addEventListener('click', exitSentenceLoop);
  }

  // Audio Engine Events
  audio.addEventListener('play', () => {
    updatePlayPauseIcons(true);
    if ('mediaSession' in navigator) {
      navigator.mediaSession.playbackState = 'playing';
    }
    if (state.currentActiveShow && state.currentActiveEpisodeMeta) {
      updateMediaSession(state.currentActiveShow, state.currentActiveEpisodeMeta);
    }
    updateMediaPositionState();
  });

  audio.addEventListener('pause', () => {
    updatePlayPauseIcons(false);
    if ('mediaSession' in navigator) {
      navigator.mediaSession.playbackState = 'paused';
    }
    updateMediaPositionState();
    throttleSaveProgress(true);
  });

  audio.addEventListener('ended', () => {
    updatePlayPauseIcons(false);
    if ('mediaSession' in navigator) {
      navigator.mediaSession.playbackState = 'paused';
    }
    updateMediaPositionState();
    if (state.currentShow && state.currentActiveEpisodeMeta) {
      saveEpisodeProgress(state.currentShow.id, state.currentActiveEpisodeMeta.guid, audio.currentTime, audio.duration, true);
      if (updateEpisodeProgressCallback) {
        updateEpisodeProgressCallback(state.currentShow.id, state.currentActiveEpisodeMeta.guid);
      }
    }
  });

  audio.addEventListener('ratechange', () => {
    updateMediaPositionState();
  });

  audio.addEventListener('loadedmetadata', () => {
    const dur = audio.duration || (state.currentEpisode ? state.currentEpisode.duration_sec : 0);
    timeTotal.textContent = formatTime(dur);
    seekSlider.max = dur;
    if (state.currentActiveShow && state.currentActiveEpisodeMeta) {
      updateMediaSession(state.currentActiveShow, state.currentActiveEpisodeMeta);
    }
    updateMediaPositionState();
    if (state.currentEpisode && state.currentEpisode.highlights && state.currentEpisode.highlights.length > 0) {
      renderChapterMarkers(state.currentEpisode.highlights);
    }
  });

  audio.addEventListener('timeupdate', () => {
    if (!state.currentEpisode) return;

    const curTime = audio.currentTime;
    const totalDur = (Number.isFinite(audio.duration) && audio.duration > 0)
      ? audio.duration
      : (state.currentEpisode.duration_sec || 0);

    if (!state.isScrubbing) {
      seekSlider.value = curTime;
      timeCurrent.textContent = formatTime(curTime);
      updateMastheadPlayhead(curTime, totalDur);
    }

    throttleSaveProgress(false);
    updateMediaPositionState();

    // M8-A: Single-Sentence Loop driving
    if (state.loopState.active) {
      if (curTime >= state.loopState.endSec - 0.15) {
        audio.currentTime = state.loopState.startSec;
        if (audio.paused) {
          audio.play().catch(err => console.warn('[AIRSHOW][UI] Loop play rejected:', err));
        }
      }
    }

    const activeTrIdx = binarySearchActiveIndex(state.currentEpisode.transcript, curTime);
    if (activeTrIdx !== state.lastActiveTranscriptIndex) {
      if (state.lastActiveTranscriptIndex !== -1 && state.transcriptElements[state.lastActiveTranscriptIndex]) {
        state.transcriptElements[state.lastActiveTranscriptIndex].classList.remove('active');
      }
      if (activeTrIdx !== -1 && state.transcriptElements[activeTrIdx]) {
        const activeTrEl = state.transcriptElements[activeTrIdx];
        activeTrEl.classList.add('active');

        if (state.isAutoScrollEnabled) {
          activeTrEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        const speaker = state.currentEpisode.transcript[activeTrIdx].speaker || 'Air Show';
        if (playerSpeakerName) playerSpeakerName.textContent = speaker;
      }
      state.lastActiveTranscriptIndex = activeTrIdx;
    }

    const activeHlIdx = binarySearchActiveIndex(state.currentEpisode.highlights, curTime);
    if (activeHlIdx !== state.lastActiveHighlightIndex) {
      if (state.lastActiveHighlightIndex !== -1 && state.highlightElements[state.lastActiveHighlightIndex]) {
        state.highlightElements[state.lastActiveHighlightIndex].classList.remove('active');
      }
      if (activeHlIdx !== -1 && state.highlightElements[activeHlIdx]) {
        state.highlightElements[activeHlIdx].classList.add('active');
      }
      if (elements.mastheadChapterTicks) {
        const ticks = elements.mastheadChapterTicks.querySelectorAll('.masthead-chapter-tick');
        ticks.forEach((tick, i) => tick.classList.toggle('active', i === activeHlIdx));
      }
      state.lastActiveHighlightIndex = activeHlIdx;
    }

    // Realtime chapter badge update
    const activeHl = (state.currentEpisode.highlights && activeHlIdx !== -1) ? state.currentEpisode.highlights[activeHlIdx] : null;
    if (activeHl && activeHl.title_zh) {
      playerChapterTitle.textContent = activeHl.title_zh;
      playerChapterBadge.style.display = 'inline-flex';
      playerChapterBadge.title = `当前章节：${activeHl.title_zh} (点击跳转)`;
    } else {
      playerChapterBadge.style.display = 'none';
    }
  });

  seekSlider.addEventListener('input', () => {
    state.isScrubbing = true;
    const val = Number(seekSlider.value);
    timeCurrent.textContent = formatTime(val);
    const dur = (Number.isFinite(audio.duration) && audio.duration > 0)
      ? audio.duration
      : (state.currentEpisode ? state.currentEpisode.duration_sec : 0);
    updateMastheadPlayhead(val, dur);
  });

  seekSlider.addEventListener('change', () => {
    state.isScrubbing = false;
    seekAndPlay(Number(seekSlider.value));
    updateMediaPositionState();
  });

  // Live Masthead Acoustic Ruler Click-to-Seek
  if (elements.mastheadRulerTrack) {
    elements.mastheadRulerTrack.addEventListener('click', (e) => {
      const rect = elements.mastheadRulerTrack.getBoundingClientRect();
      if (!rect.width) return;
      const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const dur = (Number.isFinite(audio.duration) && audio.duration > 0)
        ? audio.duration
        : (state.currentEpisode && state.currentEpisode.duration_sec) ||
          (state.currentActiveEpisodeMeta && state.currentActiveEpisodeMeta.duration_sec) || 0;
      if (dur > 0) {
        const targetSec = ratio * dur;
        updateMastheadPlayhead(targetSec, dur);
        seekAndPlay(targetSec);
      }
    });
  }

  // M7 Realtime Chapter Badge Interaction
  if (playerChapterBadge) {
    playerChapterBadge.addEventListener('click', () => {
      if (state.lastActiveHighlightIndex !== -1 && state.highlightElements[state.lastActiveHighlightIndex]) {
        const el = state.highlightElements[state.lastActiveHighlightIndex];
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('flash-focus');
        setTimeout(() => el.classList.remove('flash-focus'), 1200);
      }
    });
    playerChapterBadge.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        playerChapterBadge.click();
      }
    });
  }

  // M7 Resume Banner Actions
  if (btnResumePlay) {
    btnResumePlay.addEventListener('click', () => {
      if (resumeBanner) resumeBanner.style.display = 'none';
      audio.play().catch(err => console.warn('[AIRSHOW][UI] Play rejected:', err));
    });
  }

  if (btnResumeRestart) {
    btnResumeRestart.addEventListener('click', () => {
      if (resumeBanner) resumeBanner.style.display = 'none';
      state.userInteractedSeek = true;
      seekAndPlay(0);
      if (state.currentShow && state.currentActiveEpisodeMeta) {
        saveEpisodeProgress(state.currentShow.id, state.currentActiveEpisodeMeta.guid, 0, state.currentActiveEpisodeMeta.duration_sec, false);
        if (updateEpisodeProgressCallback) {
          updateEpisodeProgressCallback(state.currentShow.id, state.currentActiveEpisodeMeta.guid);
        }
      }
    });
  }

  if (btnResumeClose) {
    btnResumeClose.addEventListener('click', () => {
      if (resumeBanner) resumeBanner.style.display = 'none';
    });
  }

  // M7 Progress Persistence Flush Triggers
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      throttleSaveProgress(true);
    }
  });

  window.addEventListener('beforeunload', () => {
    throttleSaveProgress(true);
  });
}
