/**
 * shortcuts.js - Global keyboard shortcuts & help modal
 * Handles: Space, ArrowLeft, ArrowRight, Shift+Arrows, R, L, B, /, ?, Esc, Cmd/Ctrl+K
 */

import { state, elements } from './state.js';
import { binarySearchActiveIndex } from './utils.js';
import { seekAndPlay, togglePlayPause, rewind, fastForward, startSentenceLoop, exitSentenceLoop } from './player.js';
import { applySearchFilter, isCompactSearchActive, toggleMobileSearch, cycleBlurMode } from './reader.js';
import { closeArchiveDrawer } from './archive.js';

export function openShortcutsModal() {
  const { shortcutsModal, shortcutsBackdrop, btnKeyboardShortcuts } = elements;
  if (shortcutsModal) shortcutsModal.classList.add('visible');
  if (shortcutsBackdrop) shortcutsBackdrop.classList.add('visible');
  if (btnKeyboardShortcuts) btnKeyboardShortcuts.classList.add('active');
}

export function closeShortcutsModal() {
  const { shortcutsModal, shortcutsBackdrop, btnKeyboardShortcuts } = elements;
  if (shortcutsModal) shortcutsModal.classList.remove('visible');
  if (shortcutsBackdrop) shortcutsBackdrop.classList.remove('visible');
  if (btnKeyboardShortcuts) btnKeyboardShortcuts.classList.remove('active');
}

export function toggleShortcutsModal() {
  const { shortcutsModal } = elements;
  if (shortcutsModal && shortcutsModal.classList.contains('visible')) {
    closeShortcutsModal();
  } else {
    openShortcutsModal();
  }
}

export function initShortcuts() {
  const { btnKeyboardShortcuts, shortcutsCloseBtn, shortcutsBackdrop, shortcutsModal, archiveDrawer, searchInput, searchBoxWrap, playerBar, audio } = elements;

  if (btnKeyboardShortcuts) {
    btnKeyboardShortcuts.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleShortcutsModal();
    });
  }
  if (shortcutsCloseBtn) {
    shortcutsCloseBtn.addEventListener('click', closeShortcutsModal);
  }
  if (shortcutsBackdrop) {
    shortcutsBackdrop.addEventListener('click', closeShortcutsModal);
  }

  window.addEventListener('keydown', (e) => {
    // 1. Esc: close modal/drawer or clear search
    if (e.key === 'Escape') {
      if (shortcutsModal && shortcutsModal.classList.contains('visible')) {
        closeShortcutsModal();
        return;
      }
      if (archiveDrawer && archiveDrawer.classList.contains('open')) {
        closeArchiveDrawer();
        return;
      }
      if (searchInput && (document.activeElement === searchInput || searchInput.value || (searchBoxWrap && searchBoxWrap.classList.contains('mobile-expanded')))) {
        searchInput.value = '';
        applySearchFilter('');
        searchInput.blur();
        if (searchBoxWrap && searchBoxWrap.classList.contains('mobile-expanded')) {
          toggleMobileSearch(false);
        }
        return;
      }
    }

    // 2. Cmd/Ctrl+K search shortcut
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (isCompactSearchActive() && searchBoxWrap) {
        toggleMobileSearch(true);
      }
      if (searchInput) {
        searchInput.focus();
        searchInput.select();
      }
      return;
    }

    // 3. Strict editable elements guard
    const activeEl = document.activeElement;
    if (activeEl) {
      const tagName = activeEl.tagName;
      if (
        tagName === 'TEXTAREA' ||
        tagName === 'SELECT' ||
        activeEl.isContentEditable ||
        (tagName === 'INPUT' && activeEl.type !== 'range')
      ) {
        return;
      }
    }

    // 4. Strict modifier keys filter (ctrl, meta, alt)
    if (e.ctrlKey || e.metaKey || e.altKey) {
      return;
    }

    // 5. '/' search shortcut
    if (e.key === '/') {
      e.preventDefault();
      if (isCompactSearchActive() && searchBoxWrap) {
        toggleMobileSearch(true);
      }
      if (searchInput) {
        searchInput.focus();
        searchInput.select();
      }
      return;
    }

    // 6. '?' toggle shortcuts help modal
    if (e.key === '?' || (e.shiftKey && (e.key === '/' || e.code === 'Slash'))) {
      e.preventDefault();
      toggleShortcutsModal();
      return;
    }

    // 7. KeyB: Cycle blur listening mode (清晰 -> 遮中文 -> 全盲听)
    if (e.code === 'KeyB' || e.key === 'b' || e.key === 'B') {
      e.preventDefault();
      cycleBlurMode();
      return;
    }

    // Playback control guards: empty state, hidden player bar, no audio loaded, or drawer open
    if (!playerBar || playerBar.style.display === 'none' || !audio || !audio.src || !state.currentEpisode || (archiveDrawer && archiveDrawer.classList.contains('open'))) {
      return;
    }

    // 8. Space: Toggle play/pause
    if (e.code === 'Space' || e.key === ' ') {
      if (e.repeat) return;
      e.preventDefault();
      togglePlayPause();
      return;
    }

    // 9. ArrowLeft / ArrowRight: Rewind / Fast-forward 5s (Shift for ±15s)
    if (e.code === 'ArrowLeft') {
      e.preventDefault();
      const step = e.shiftKey ? 15 : 5;
      rewind(step);
      return;
    }
    if (e.code === 'ArrowRight') {
      e.preventDefault();
      const step = e.shiftKey ? 15 : 5;
      fastForward(step);
      return;
    }

    // 10. KeyR: Replay current active transcript item (jump back to start_sec or -5s)
    if (e.code === 'KeyR' || e.key === 'r' || e.key === 'R') {
      e.preventDefault();
      let targetSec = -1;
      if (state.lastActiveTranscriptIndex !== -1 && state.currentEpisode && state.currentEpisode.transcript && state.currentEpisode.transcript[state.lastActiveTranscriptIndex]) {
        targetSec = state.currentEpisode.transcript[state.lastActiveTranscriptIndex].start_sec;
      } else if (state.currentEpisode && state.currentEpisode.transcript && state.currentEpisode.transcript.length > 0) {
        const foundIdx = binarySearchActiveIndex(state.currentEpisode.transcript, audio.currentTime);
        if (foundIdx !== -1 && state.currentEpisode.transcript[foundIdx]) {
          targetSec = state.currentEpisode.transcript[foundIdx].start_sec;
        }
      }
      if (targetSec >= 0) {
        seekAndPlay(targetSec);
      } else {
        seekAndPlay(Math.max(0, audio.currentTime - 5));
      }
      return;
    }

    // 11. KeyL: Toggle single-sentence loop for current active transcript item
    if (e.code === 'KeyL' || e.key === 'l' || e.key === 'L') {
      e.preventDefault();
      if (state.loopState.active) {
        exitSentenceLoop();
      } else {
        let targetIdx = state.lastActiveTranscriptIndex;
        if (targetIdx === -1 && state.currentEpisode && state.currentEpisode.transcript && state.currentEpisode.transcript.length > 0) {
          targetIdx = binarySearchActiveIndex(state.currentEpisode.transcript, audio.currentTime);
        }
        if (targetIdx !== -1) {
          startSentenceLoop(targetIdx);
        }
      }
      return;
    }
  });
}
