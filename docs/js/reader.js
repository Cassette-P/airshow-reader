/**
 * reader.js - Article masthead, executive summary, audio highlights,
 *             knowledge cards, bilingual transcript, search filter & blur listening
 */

import { state, elements, isCompactLayout } from './state.js';
import { getStoredBlurMode, setStoredBlurMode } from './storage.js';
import { escapeHtml, formatTime, formatDurationZh, formatDateZh, getSpeakerCssClass, binarySearchActiveIndex } from './utils.js';
import { seekAndPlay, exitSentenceLoop, toggleSentenceLoop, renderChapterMarkers } from './player.js';
import { closeReaderPrefs } from './theme.js';

/* ==========================================================================
   Compact / Mobile 3-Tab View Controller (overview | transcript | notes)
   ========================================================================== */
export function expandCompactKnowledgeAccordions() {
  if (!isCompactLayout()) return;
  const { vocabAccordion, referencesAccordion } = elements;
  if (vocabAccordion && vocabAccordion.style.display !== 'none') {
    vocabAccordion.open = true;
  }
  if (referencesAccordion && referencesAccordion.style.display !== 'none') {
    referencesAccordion.open = true;
  }
}

export function updateMobileTabBadges(overviewCount = 0, transcriptCount = 0) {
  const { tabOverviewBadges, tabTranscriptBadges } = elements;
  if (tabOverviewBadges) {
    tabOverviewBadges.forEach(badge => {
      badge.textContent = overviewCount > 0 ? String(overviewCount) : '';
      badge.classList.toggle('has-count', overviewCount > 0);
    });
  }
  if (tabTranscriptBadges) {
    tabTranscriptBadges.forEach(badge => {
      badge.textContent = transcriptCount > 0 ? String(transcriptCount) : '';
      badge.classList.toggle('has-count', transcriptCount > 0);
    });
  }
}

export function getAvailableMobileTabs() {
  return state.hasKnowledgeNotes ? ['overview', 'transcript', 'notes'] : ['overview', 'transcript'];
}

export function setActiveMobileTab(tab, options = {}) {
  const { restoreScroll = true, scrollTop = false, swipeDirection = null } = options;
  let targetTab = ['overview', 'transcript', 'notes'].includes(tab) ? tab : 'overview';
  if (targetTab === 'notes' && !state.hasKnowledgeNotes) {
    targetTab = 'overview';
  }

  const prevTab = state.activeMobileTab;
  if (isCompactLayout() && prevTab && prevTab !== targetTab) {
    state.tabScrollPositions[prevTab] = window.scrollY;
  }

  state.activeMobileTab = targetTab;
  if (elements.episodeContent) {
    elements.episodeContent.setAttribute('data-active-tab', targetTab);
  }
  if (elements.mobileTabBtns) {
    elements.mobileTabBtns.forEach(btn => {
      const isActive = btn.dataset.tab === targetTab;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
  }

  if (targetTab === 'notes') {
    expandCompactKnowledgeAccordions();
  }

  if (isCompactLayout() && prevTab !== targetTab) {
    if (swipeDirection && elements.episodeContent) {
      const animClass = swipeDirection === 'next' ? 'tab-enter-from-right' : 'tab-enter-from-left';
      const panels = elements.episodeContent.querySelectorAll(`[data-tab-panel="${targetTab}"]`);
      panels.forEach(panel => {
        panel.classList.remove('tab-enter-from-right', 'tab-enter-from-left');
        void panel.offsetWidth;
        panel.classList.add(animClass);
        panel.addEventListener('animationend', () => {
          panel.classList.remove('tab-enter-from-right', 'tab-enter-from-left');
        }, { once: true });
      });
    }

    if (scrollTop) {
      window.scrollTo({ top: 0, behavior: 'auto' });
    } else if (restoreScroll) {
      const savedY = state.tabScrollPositions[targetTab] || 0;
      requestAnimationFrame(() => {
        window.scrollTo({ top: savedY, behavior: 'auto' });
      });
    }
  }
}

export function resetMobileTabs(defaultTab = 'overview') {
  state.tabScrollPositions = { overview: 0, transcript: 0, notes: 0 };
  setActiveMobileTab(defaultTab, { restoreScroll: false, scrollTop: false });
}

export function jumpToTranscriptAt(startSec) {
  if (state.loopState.active) {
    exitSentenceLoop();
  }
  if (isCompactLayout()) {
    setActiveMobileTab('transcript', { restoreScroll: false });
  }
  seekAndPlay(startSec);

  const tr = (state.currentEpisode && state.currentEpisode.transcript) || [];
  const idx = binarySearchActiveIndex(tr, Number(startSec) || 0);
  if (idx !== -1 && state.transcriptElements[idx]) {
    requestAnimationFrame(() => {
      state.transcriptElements[idx].scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }
}

export function renderArticleMeta(show, ep) {
  if (elements.metaPodcastTitle) elements.metaPodcastTitle.textContent = (show && show.title) || '播客精读';
  if (elements.metaPubDate) elements.metaPubDate.textContent = formatDateZh(ep.pub_date);
  if (elements.metaDuration) elements.metaDuration.textContent = formatDurationZh(ep.duration_sec);
  if (elements.metaAudioLink) {
    const rawAudioUrl = typeof ep.audio_url === 'string' ? ep.audio_url.trim() : '';
    elements.metaAudioLink.href = /^https?:\/\//i.test(rawAudioUrl) ? rawAudioUrl : '#';
  }
  if (elements.episodeTitle) elements.episodeTitle.textContent = ep.title || '';

  const dur = Number(ep.duration_sec) || 0;
  if (elements.mastheadMicroTicks) {
    const tickCount = 21;
    let html = '';
    for (let i = 0; i < tickCount; i++) {
      const isMajor = i % 5 === 0;
      html += `<span class="ruler-tick${isMajor ? ' major' : ''}"></span>`;
    }
    elements.mastheadMicroTicks.innerHTML = html;
  }
  if (elements.mastheadRulerProgress) {
    elements.mastheadRulerProgress.style.transform = 'translateY(-50%) scaleX(0)';
  }
  if (elements.mastheadPlayhead) {
    elements.mastheadPlayhead.style.left = '0%';
    elements.mastheadPlayhead.classList.remove('visible');
  }

  const ticksEl = document.getElementById('masthead-ticks') || document.querySelector('.masthead-ticks');
  if (ticksEl) {
    ticksEl.innerHTML = `
      <span class="tick-label">${formatTime(0)}</span>
      <span class="tick-label">${formatTime(dur * 0.25)}</span>
      <span class="tick-label">${formatTime(dur * 0.5)}</span>
      <span class="tick-label">${formatTime(dur * 0.75)}</span>
      <span class="tick-label" id="masthead-scale-end">${formatTime(dur)}</span>
    `;
  } else if (elements.mastheadScaleEnd) {
    elements.mastheadScaleEnd.textContent = formatTime(dur);
  }
}

export function renderExecutiveSummary(ep) {
  if (elements.summaryZhText) {
    elements.summaryZhText.textContent = ep.summary_zh || '暂无摘要';
  }

  const takeaways = ep.key_takeaways_zh || [];
  if (elements.takeawaysCountBadge) {
    elements.takeawaysCountBadge.textContent = takeaways.length > 0 ? `${takeaways.length} 条洞察` : '';
  }

  if (elements.takeawaysList) {
    elements.takeawaysList.innerHTML = '';
    takeaways.forEach((item, idx) => {
      const li = document.createElement('li');
      li.className = 'takeaway-item';

      const num = document.createElement('span');
      num.className = 'takeaway-num tnum';
      num.textContent = `${idx + 1}.`;

      const text = document.createElement('span');
      text.textContent = item;

      li.appendChild(num);
      li.appendChild(text);
      elements.takeawaysList.appendChild(li);
    });
  }
}

export function renderHighlights(highlights) {
  if (!elements.highlightsGrid) return;
  elements.highlightsGrid.innerHTML = '';
  state.highlightElements = [];
  if (elements.highlightsCountBadge) {
    elements.highlightsCountBadge.textContent = `${highlights.length} 个片段`;
  }

  highlights.forEach((hl, idx) => {
    const card = document.createElement('div');
    card.className = 'highlight-card';
    card.dataset.startSec = hl.start_sec;
    card.dataset.index = idx;

    const header = document.createElement('div');
    header.className = 'highlight-header';

    const timeBadge = document.createElement('span');
    timeBadge.className = 'timestamp-badge tnum';
    timeBadge.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <polygon points="6 4 20 12 6 20 6 4"></polygon>
      </svg>
      ${formatTime(hl.start_sec)}
    `;

    const playingIndicator = document.createElement('span');
    playingIndicator.className = 'playing-indicator tnum';
    playingIndicator.textContent = '正在播放';

    header.appendChild(timeBadge);
    header.appendChild(playingIndicator);
    card.appendChild(header);

    const title = document.createElement('div');
    title.className = 'highlight-title';
    title.textContent = hl.title_zh || '';
    card.appendChild(title);

    const summary = document.createElement('div');
    summary.className = 'highlight-summary';
    summary.textContent = hl.summary_zh || '';
    card.appendChild(summary);

    if (hl.quote_en) {
      const quote = document.createElement('blockquote');
      quote.className = 'highlight-quote';
      quote.textContent = `“${hl.quote_en}”`;
      card.appendChild(quote);
    }

    const jumpBtn = document.createElement('button');
    jumpBtn.type = 'button';
    jumpBtn.className = 'highlight-jump-btn';
    jumpBtn.textContent = '定位逐字稿 →';
    jumpBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      jumpToTranscriptAt(hl.start_sec);
    });
    card.appendChild(jumpBtn);

    card.addEventListener('click', () => {
      if (state.loopState.active) {
        exitSentenceLoop();
      }
      seekAndPlay(hl.start_sec);
    });

    elements.highlightsGrid.appendChild(card);
    state.highlightElements.push(card);
  });

  renderChapterMarkers(highlights);
}

export function getCategoryLabel(category) {
  const catMap = {
    book: '书籍',
    article: '文章/报告',
    media: '影音/播客',
    other: '其他'
  };
  const key = category ? String(category).toLowerCase().trim() : '';
  return catMap[key] || '其他';
}

export function resetKnowledgeCards() {
  state.hasKnowledgeNotes = false;
  document.body.setAttribute('data-has-notes', 'false');
  if (state.activeMobileTab === 'notes') {
    setActiveMobileTab('overview', { restoreScroll: false });
  }
  if (!elements.knowledgeSection) return;
  elements.knowledgeSection.style.display = 'none';
  if (elements.vocabAccordion) {
    elements.vocabAccordion.style.display = 'none';
    elements.vocabAccordion.open = false;
  }
  if (elements.referencesAccordion) {
    elements.referencesAccordion.style.display = 'none';
    elements.referencesAccordion.open = false;
  }
  if (elements.vocabGrid) elements.vocabGrid.innerHTML = '';
  if (elements.referencesList) elements.referencesList.innerHTML = '';
}

export function renderKnowledgeCards(detail) {
  if (!elements.knowledgeSection) return;

  const vocabNotes = Array.isArray(detail && detail.vocab_notes) ? detail.vocab_notes : [];
  const references = Array.isArray(detail && detail.references) ? detail.references : [];

  const hasVocab = vocabNotes.length > 0;
  const hasRefs = references.length > 0;

  if (!hasVocab && !hasRefs) {
    resetKnowledgeCards();
    return;
  }

  state.hasKnowledgeNotes = true;
  document.body.setAttribute('data-has-notes', 'true');
  elements.knowledgeSection.style.display = 'block';

  // Update Vocab Notes Accordion
  if (elements.vocabAccordion && elements.vocabGrid) {
    if (hasVocab) {
      elements.vocabAccordion.style.display = 'block';
      elements.vocabAccordion.open = false;
      if (elements.vocabCountText) {
        elements.vocabCountText.textContent = `${vocabNotes.length} 条`;
      }
      elements.vocabGrid.innerHTML = '';
      vocabNotes.forEach(item => {
        const card = document.createElement('div');
        card.className = 'vocab-card';

        const header = document.createElement('div');
        header.className = 'vocab-card-header';

        const termEl = document.createElement('span');
        termEl.className = 'vocab-term';
        termEl.textContent = item.term || '';
        header.appendChild(termEl);

        if (item.start_sec !== null && item.start_sec !== undefined && Number.isFinite(Number(item.start_sec))) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'timestamp-badge tnum vocab-timestamp-btn';
          const timeStr = formatTime(Number(item.start_sec));
          btn.title = `定位至 ${timeStr} 播放`;
          btn.setAttribute('aria-label', `定位至 ${timeStr} 播放`);
          btn.innerHTML = `
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <polygon points="6 4 20 12 6 20 6 4"></polygon>
            </svg>
            ${timeStr}
          `;
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (state.loopState && state.loopState.active) {
              exitSentenceLoop();
            }
            seekAndPlay(Number(item.start_sec));
          });
          header.appendChild(btn);
        }

        card.appendChild(header);

        const body = document.createElement('div');
        body.className = 'vocab-card-body';

        if (item.meaning_zh) {
          const meaning = document.createElement('div');
          meaning.className = 'vocab-meaning';
          meaning.textContent = item.meaning_zh;
          body.appendChild(meaning);
        }

        if (item.note_zh) {
          const note = document.createElement('div');
          note.className = 'vocab-note';
          note.textContent = item.note_zh;
          body.appendChild(note);
        }

        card.appendChild(body);
        elements.vocabGrid.appendChild(card);
      });
    } else {
      elements.vocabAccordion.style.display = 'none';
      elements.vocabAccordion.open = false;
      elements.vocabGrid.innerHTML = '';
    }
  }

  // Update References Accordion
  if (elements.referencesAccordion && elements.referencesList) {
    if (hasRefs) {
      elements.referencesAccordion.style.display = 'block';
      elements.referencesAccordion.open = false;
      if (elements.referencesCountText) {
        elements.referencesCountText.textContent = `${references.length} 项`;
      }
      elements.referencesList.innerHTML = '';
      references.forEach(item => {
        const row = document.createElement('div');
        row.className = 'ref-item';

        const header = document.createElement('div');
        header.className = 'ref-item-header';

        const metaGroup = document.createElement('div');
        metaGroup.className = 'ref-meta-group';

        const catBadge = document.createElement('span');
        catBadge.className = 'ref-category-badge';
        catBadge.textContent = getCategoryLabel(item.category);
        metaGroup.appendChild(catBadge);

        const titleLine = document.createElement('div');
        titleLine.className = 'ref-title-line';

        const titleStrong = document.createElement('strong');
        titleStrong.className = 'ref-title';
        titleStrong.textContent = item.title || '';
        titleLine.appendChild(titleStrong);

        const creatorStr = item.creator != null ? String(item.creator).trim() : '';
        if (creatorStr) {
          const creatorSpan = document.createElement('span');
          creatorSpan.className = 'ref-creator';
          creatorSpan.textContent = ` · ${creatorStr}`;
          titleLine.appendChild(creatorSpan);
        }

        metaGroup.appendChild(titleLine);
        header.appendChild(metaGroup);

        if (item.start_sec !== null && item.start_sec !== undefined && Number.isFinite(Number(item.start_sec))) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'timestamp-badge tnum ref-timestamp-btn';
          const timeStr = formatTime(Number(item.start_sec));
          btn.title = `定位至 ${timeStr} 播放`;
          btn.setAttribute('aria-label', `定位至 ${timeStr} 播放`);
          btn.innerHTML = `
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <polygon points="6 4 20 12 6 20 6 4"></polygon>
            </svg>
            ${timeStr}
          `;
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (state.loopState && state.loopState.active) {
              exitSentenceLoop();
            }
            seekAndPlay(Number(item.start_sec));
          });
          header.appendChild(btn);
        }

        row.appendChild(header);

        if (item.note_zh) {
          const note = document.createElement('div');
          note.className = 'ref-note';
          note.textContent = item.note_zh;
          row.appendChild(note);
        }

        if (Array.isArray(item.links) && item.links.length > 0) {
          const validLinks = item.links.filter(
            (lnk) => lnk && typeof lnk.url === 'string' && /^https?:\/\//i.test(lnk.url.trim())
          );
          if (validLinks.length > 0) {
            const linksRow = document.createElement('div');
            linksRow.className = 'ref-links-row';
            const refTitleText = String(item.title || '').trim();

            validLinks.forEach((lnk) => {
              const cleanUrl = lnk.url.trim();
              const labelText = String(lnk.label || lnk.source || '链接').trim();
              const sourceKey = String(lnk.source || '').trim();

              const pill = document.createElement('a');
              pill.className = 'ref-link-pill';
              pill.href = cleanUrl;
              pill.target = '_blank';
              pill.rel = 'noopener noreferrer';
              if (sourceKey) {
                pill.dataset.source = sourceKey;
              }
              const ariaPrefix = ['article', 'official', 'podcast_site'].includes(sourceKey)
                ? `打开《${refTitleText}》${labelText}`
                : `在${labelText}查看《${refTitleText}》`;
              pill.setAttribute('aria-label', `${ariaPrefix}（新窗口打开）`);

              const labelSpan = document.createElement('span');
              labelSpan.textContent = labelText;
              pill.appendChild(labelSpan);

              pill.insertAdjacentHTML(
                'beforeend',
                '<svg class="ref-link-icon" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="7" y1="17" x2="17" y2="7"></line><polyline points="7 7 17 7 17 17"></polyline></svg>'
              );

              pill.addEventListener('click', (e) => {
                e.stopPropagation();
              });

              linksRow.appendChild(pill);
            });

            row.appendChild(linksRow);
          }
        }

        elements.referencesList.appendChild(row);
      });
    } else {
      elements.referencesAccordion.style.display = 'none';
      elements.referencesAccordion.open = false;
      elements.referencesList.innerHTML = '';
    }
  }

  // Section badge summary
  if (elements.knowledgeTotalBadge) {
    if (hasVocab && hasRefs) {
      elements.knowledgeTotalBadge.textContent = `${vocabNotes.length} 语料 · ${references.length} 资源`;
    } else if (hasVocab) {
      elements.knowledgeTotalBadge.textContent = `${vocabNotes.length} 条语料`;
    } else {
      elements.knowledgeTotalBadge.textContent = `${references.length} 项资源`;
    }
  }

  if (state.activeMobileTab === 'notes') {
    expandCompactKnowledgeAccordions();
  }
}

export function setBlurMode(mode, save = true) {
  if (!['off', 'zh', 'both'].includes(mode)) mode = 'off';
  state.currentBlurMode = mode;
  if (save) {
    setStoredBlurMode(mode);
  }
  document.body.setAttribute('data-blur-mode', mode);

  if (elements.blurSegmentBtns) {
    elements.blurSegmentBtns.forEach(btn => {
      btn.classList.toggle('active', btn.dataset.blur === mode);
    });
  }

  clearRevealedStates();
}

export function cycleBlurMode() {
  const modes = ['off', 'zh', 'both'];
  const curIdx = modes.indexOf(state.currentBlurMode);
  const nextMode = modes[(curIdx + 1) % modes.length];
  setBlurMode(nextMode, true);
}

export function clearRevealedStates() {
  if (!elements.transcriptContainer) return;
  const revealedEls = elements.transcriptContainer.querySelectorAll('.is-revealed');
  revealedEls.forEach(el => el.classList.remove('is-revealed'));
}

export function initBlurMode() {
  const saved = getStoredBlurMode();
  setBlurMode(saved, false);

  if (elements.blurSegmentBtns) {
    elements.blurSegmentBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        setBlurMode(btn.dataset.blur, true);
      });
    });
  }
}

export function handleTranscriptTextClick(e, lang, textEl) {
  const row = textEl.closest('.transcript-item');
  if (row && row.classList.contains('has-search-match')) {
    return;
  }
  const isTargetBlurred = (state.currentBlurMode === 'both') || (state.currentBlurMode === 'zh' && lang === 'zh');
  if (isTargetBlurred) {
    e.stopPropagation();
    textEl.classList.toggle('is-revealed');
  }
}

export function renderTranscript(transcript) {
  if (!elements.transcriptContainer) return;
  elements.transcriptContainer.innerHTML = '';
  state.transcriptElements = [];
  if (elements.transcriptCountBadge) {
    elements.transcriptCountBadge.textContent = `${transcript.length} 段落`;
  }

  transcript.forEach((item, idx) => {
    const row = document.createElement('div');
    row.className = 'transcript-item';
    row.dataset.startSec = item.start_sec;
    row.dataset.index = idx;
    row.dataset.id = item.id;

    const meta = document.createElement('div');
    meta.className = 'transcript-meta';

    const metaLeft = document.createElement('div');
    metaLeft.className = 'meta-left';

    const speaker = document.createElement('span');
    speaker.className = `speaker-badge ${getSpeakerCssClass(item.speaker)}`;
    speaker.textContent = item.speaker || 'Guest';
    metaLeft.appendChild(speaker);

    const timeBtn = document.createElement('button');
    timeBtn.type = 'button';
    timeBtn.className = 'time-seek-btn tnum';
    timeBtn.title = `跳转至 ${formatTime(item.start_sec)}`;
    timeBtn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <polygon points="6 4 20 12 6 20 6 4"></polygon>
      </svg>
      ${formatTime(item.start_sec)}
    `;
    timeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (state.loopState.active && state.loopState.index !== idx) {
        exitSentenceLoop();
      }
      seekAndPlay(item.start_sec);
    });
    metaLeft.appendChild(timeBtn);

    // Single-Sentence Loop Button (positioned on right side of meta row)
    const loopBtn = document.createElement('button');
    loopBtn.type = 'button';
    loopBtn.className = 'btn-sentence-loop';
    loopBtn.title = '单句复读';
    loopBtn.setAttribute('aria-label', '单句复读');
    loopBtn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="17 1 21 5 17 9"></polyline>
        <path d="M3 11V9a4 4 0 0 1 4-4h14"></path>
        <polyline points="7 23 3 19 7 15"></polyline>
        <path d="M21 13v2a4 4 0 0 1-4 4H3"></path>
      </svg>
      <span>复读</span>
    `;
    loopBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleSentenceLoop(idx);
    });

    meta.appendChild(metaLeft);
    meta.appendChild(loopBtn);
    row.appendChild(meta);

    const enText = document.createElement('div');
    enText.className = 'transcript-en';
    enText.textContent = item.en || '';
    enText.addEventListener('click', (e) => {
      handleTranscriptTextClick(e, 'en', enText);
    });
    row.appendChild(enText);

    const zhText = document.createElement('div');
    zhText.className = 'transcript-zh';
    zhText.textContent = item.zh || '';
    zhText.addEventListener('click', (e) => {
      handleTranscriptTextClick(e, 'zh', zhText);
    });
    row.appendChild(zhText);

    // Sync loop active state if re-rendered
    if (state.loopState.active && state.loopState.index === idx) {
      row.classList.add('is-looping');
      loopBtn.classList.add('is-looping');
    }

    row.addEventListener('click', () => {
      if (state.loopState.active && state.loopState.index !== idx) {
        exitSentenceLoop();
      }
      seekAndPlay(item.start_sec);
    });

    elements.transcriptContainer.appendChild(row);
    state.transcriptElements.push(row);
  });
}

export function highlightTextMatches(rawText, query) {
  if (!query || !rawText) return escapeHtml(rawText);
  const escapedQ = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`(${escapedQ})`, 'gi');
  const parts = rawText.split(regex);
  return parts.map(part => {
    if (part.toLowerCase() === query.toLowerCase()) {
      return `<mark class="search-match">${escapeHtml(part)}</mark>`;
    }
    return escapeHtml(part);
  }).join('');
}

export function applySearchFilter(query) {
  if (!state.currentEpisode) return;
  const { searchClearBtn, searchStatsBanner, searchStatsText } = elements;

  if (!query) {
    if (searchClearBtn) searchClearBtn.style.display = 'none';
    if (searchStatsBanner) searchStatsBanner.classList.remove('visible');
    updateMobileTabBadges(0, 0);

    state.transcriptElements.forEach((el, idx) => {
      el.classList.remove('is-search-hidden');
      el.classList.remove('has-search-match');
      const item = (state.currentEpisode.transcript || [])[idx];
      if (item) {
        const enEl = el.querySelector('.transcript-en');
        const zhEl = el.querySelector('.transcript-zh');
        if (enEl) enEl.innerHTML = escapeHtml(item.en);
        if (zhEl) zhEl.innerHTML = escapeHtml(item.zh);
      }
    });

    state.highlightElements.forEach((el, idx) => {
      el.classList.remove('is-search-hidden');
      const item = (state.currentEpisode.highlights || [])[idx];
      if (item) {
        const titleEl = el.querySelector('.highlight-title');
        const summaryEl = el.querySelector('.highlight-summary');
        if (titleEl) titleEl.innerHTML = escapeHtml(item.title_zh);
        if (summaryEl) summaryEl.innerHTML = escapeHtml(item.summary_zh);
        const quoteEl = el.querySelector('.highlight-quote');
        if (quoteEl && item.quote_en) {
          quoteEl.innerHTML = `“${escapeHtml(item.quote_en)}”`;
        }
      }
    });
    return;
  }

  if (searchClearBtn) searchClearBtn.style.display = 'flex';
  const qLower = query.toLowerCase();
  let transcriptMatchCount = 0;
  let highlightMatchCount = 0;

  state.transcriptElements.forEach((el, idx) => {
    const item = (state.currentEpisode.transcript || [])[idx];
    if (!item) return;
    const matchEn = (item.en || '').toLowerCase().includes(qLower);
    const matchZh = (item.zh || '').toLowerCase().includes(qLower);
    const matchSpk = (item.speaker || '').toLowerCase().includes(qLower);

    if (matchEn || matchZh || matchSpk) {
      transcriptMatchCount++;
      el.classList.remove('is-search-hidden');
      el.classList.add('has-search-match');
      const enEl = el.querySelector('.transcript-en');
      const zhEl = el.querySelector('.transcript-zh');
      if (enEl) enEl.innerHTML = highlightTextMatches(item.en, query);
      if (zhEl) zhEl.innerHTML = highlightTextMatches(item.zh, query);
    } else {
      el.classList.add('is-search-hidden');
      el.classList.remove('has-search-match');
    }
  });

  state.highlightElements.forEach((el, idx) => {
    const item = (state.currentEpisode.highlights || [])[idx];
    if (!item) return;
    const matchTitle = (item.title_zh || '').toLowerCase().includes(qLower);
    const matchSummary = (item.summary_zh || '').toLowerCase().includes(qLower);
    const matchQuote = (item.quote_en || '').toLowerCase().includes(qLower);

    if (matchTitle || matchSummary || matchQuote) {
      highlightMatchCount++;
      el.classList.remove('is-search-hidden');
      const titleEl = el.querySelector('.highlight-title');
      const summaryEl = el.querySelector('.highlight-summary');
      if (titleEl) titleEl.innerHTML = highlightTextMatches(item.title_zh, query);
      if (summaryEl) summaryEl.innerHTML = highlightTextMatches(item.summary_zh, query);
      const quoteEl = el.querySelector('.highlight-quote');
      if (quoteEl && item.quote_en) {
        quoteEl.innerHTML = `“${highlightTextMatches(item.quote_en, query)}”`;
      }
    } else {
      el.classList.add('is-search-hidden');
    }
  });

  if (searchStatsText && searchStatsBanner) {
    searchStatsText.textContent = `关键词 “${query}”：匹配 ${transcriptMatchCount} 段逐字稿，${highlightMatchCount} 条精选重点`;
    searchStatsBanner.classList.add('visible');
  }
  updateMobileTabBadges(highlightMatchCount, transcriptMatchCount);
  if (isCompactLayout()) {
    if (state.activeMobileTab === 'notes') {
      if (transcriptMatchCount > 0) setActiveMobileTab('transcript', { restoreScroll: false });
      else if (highlightMatchCount > 0) setActiveMobileTab('overview', { restoreScroll: false });
    } else if (state.activeMobileTab === 'overview' && highlightMatchCount === 0 && transcriptMatchCount > 0) {
      setActiveMobileTab('transcript', { restoreScroll: false });
    } else if (state.activeMobileTab === 'transcript' && transcriptMatchCount === 0 && highlightMatchCount > 0) {
      setActiveMobileTab('overview', { restoreScroll: false });
    }
  }
}

export function isCompactSearchActive() {
  const { mobileSearchBtn } = elements;
  return !!(mobileSearchBtn && window.getComputedStyle(mobileSearchBtn).display !== 'none');
}

export function toggleMobileSearch(forceState) {
  const { searchBoxWrap, mobileSearchBtn, searchInput } = elements;
  if (!searchBoxWrap) return;
  const willExpand = forceState !== undefined ? forceState : !searchBoxWrap.classList.contains('mobile-expanded');
  if (willExpand) {
    closeReaderPrefs();
    searchBoxWrap.classList.add('mobile-expanded');
    if (mobileSearchBtn) mobileSearchBtn.classList.add('active');
    if (searchInput) {
      void searchBoxWrap.offsetWidth;
      searchInput.focus({ preventScroll: true });
    }
  } else {
    searchBoxWrap.classList.remove('mobile-expanded');
    if (mobileSearchBtn) mobileSearchBtn.classList.remove('active');
    if (searchInput) searchInput.blur();
  }
}

export function initReaderEvents() {
  const { searchInput, searchClearBtn, searchResetLink, mobileSearchBtn, searchBoxWrap, mobileTabBtns } = elements;

  if (mobileTabBtns) {
    mobileTabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.tab) {
          setActiveMobileTab(btn.dataset.tab, { restoreScroll: true });
        }
      });
    });
  }

  let searchDebounceTimer = null;
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(() => {
        applySearchFilter(searchInput.value.trim());
      }, 150);
    });
  }

  if (searchClearBtn) {
    searchClearBtn.addEventListener('click', () => {
      if (searchInput) {
        searchInput.value = '';
        applySearchFilter('');
        searchInput.focus();
      }
    });
  }

  if (searchResetLink) {
    searchResetLink.addEventListener('click', () => {
      if (searchInput) {
        searchInput.value = '';
        applySearchFilter('');
      }
    });
  }

  if (mobileSearchBtn) {
    mobileSearchBtn.addEventListener('click', () => {
      toggleMobileSearch();
    });
  }

  // Close mobile search when clicking outside
  document.addEventListener('click', (e) => {
    if (isCompactSearchActive() && searchBoxWrap && searchBoxWrap.classList.contains('mobile-expanded')) {
      if (!searchBoxWrap.contains(e.target) && mobileSearchBtn && !mobileSearchBtn.contains(e.target)) {
        toggleMobileSearch(false);
      }
    }
  });

  // Compact/Mobile Horizontal Swipe Gesture for 3-Tab Switching
  // Preserves iOS Safari edge back/forward swipes, vertical scroll & text selection
  const swipeTarget = elements.episodeContent;
  if (swipeTarget) {
    let startX = 0;
    let startY = 0;
    let startTime = 0;
    let isTracking = false;
    let swipeAxis = null; // 'x' | 'y' | null
    let activePanels = [];

    const resetPanelsTransform = (animateBack = false) => {
      activePanels.forEach(panel => {
        if (animateBack) {
          panel.style.transition = 'transform 160ms var(--ease-out)';
          panel.style.transform = '';
          setTimeout(() => {
            panel.style.transition = '';
          }, 170);
        } else {
          panel.style.transition = '';
          panel.style.transform = '';
        }
      });
      activePanels = [];
    };

    swipeTarget.addEventListener('touchstart', (e) => {
      if (!isCompactLayout() || !e.touches || e.touches.length !== 1) return;
      if (elements.archiveDrawer && elements.archiveDrawer.classList.contains('open')) return;
      if (elements.readerPrefsPopover && elements.readerPrefsPopover.classList.contains('visible')) return;

      const touch = e.touches[0];
      const vw = window.innerWidth || 390;
      // Leave 28px on both edges for iOS Safari native swipe-back/forward
      if (touch.clientX < 28 || touch.clientX > vw - 28) return;

      const t = e.target;
      if (t && t.closest('input, textarea, select, .scrub-track-wrap, .audio-player-bar, .app-header, .archive-drawer, .reader-prefs-popover, .podcast-nav-tabs, .archive-show-switcher')) {
        return;
      }

      const sel = window.getSelection && window.getSelection();
      if (sel && sel.toString().length > 0) return;

      startX = touch.clientX;
      startY = touch.clientY;
      startTime = Date.now();
      isTracking = true;
      swipeAxis = null;
      activePanels = Array.from(
        swipeTarget.querySelectorAll(`[data-tab-panel="${state.activeMobileTab}"]`)
      );
    }, { passive: true });

    swipeTarget.addEventListener('touchmove', (e) => {
      if (!isTracking || !e.touches || e.touches.length !== 1) return;
      const touch = e.touches[0];
      const dx = touch.clientX - startX;
      const dy = touch.clientY - startY;

      if (!swipeAxis) {
        if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
        if (Math.abs(dx) > Math.abs(dy) * 1.35) {
          swipeAxis = 'x';
        } else {
          swipeAxis = 'y';
          isTracking = false;
          resetPanelsTransform(false);
          return;
        }
      }

      if (swipeAxis === 'x') {
        const tabs = getAvailableMobileTabs();
        const curIdx = tabs.indexOf(state.activeMobileTab);
        const isAtBoundary = (dx > 0 && curIdx <= 0) || (dx < 0 && curIdx >= tabs.length - 1);
        const damped = isAtBoundary
          ? Math.max(-18, Math.min(18, dx * 0.09))
          : Math.max(-52, Math.min(52, dx * 0.26));
        activePanels.forEach(panel => {
          panel.style.transition = 'none';
          panel.style.transform = `translateX(${damped}px)`;
        });
      }
    }, { passive: true });

    const finishSwipe = (e) => {
      if (!isTracking) return;
      isTracking = false;

      if (swipeAxis !== 'x') {
        resetPanelsTransform(false);
        return;
      }

      const touch = e.changedTouches && e.changedTouches[0];
      if (!touch) {
        resetPanelsTransform(true);
        return;
      }

      const dx = touch.clientX - startX;
      const dy = touch.clientY - startY;
      const dt = Date.now() - startTime;
      const isValidSwipe = (Math.abs(dx) >= 56 || (Math.abs(dx) >= 34 && dt < 260)) &&
        Math.abs(dx) > Math.abs(dy) * 1.3;

      const tabs = getAvailableMobileTabs();
      const curIdx = tabs.indexOf(state.activeMobileTab);

      if (isValidSwipe && dx < 0 && curIdx < tabs.length - 1) {
        resetPanelsTransform(false);
        setActiveMobileTab(tabs[curIdx + 1], { restoreScroll: true, swipeDirection: 'next' });
      } else if (isValidSwipe && dx > 0 && curIdx > 0) {
        resetPanelsTransform(false);
        setActiveMobileTab(tabs[curIdx - 1], { restoreScroll: true, swipeDirection: 'prev' });
      } else {
        resetPanelsTransform(true);
      }
    };

    swipeTarget.addEventListener('touchend', finishSwipe, { passive: true });
    swipeTarget.addEventListener('touchcancel', () => {
      isTracking = false;
      swipeAxis = null;
      resetPanelsTransform(true);
    }, { passive: true });
  }
}
