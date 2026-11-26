/**
 * state.js - Shared runtime state, in-memory caches, anti-race locks & DOM element registry
 */

export const PLAYBACK_RATES = [1.0, 1.25, 1.5, 2.0];

export const showIndexCache = new Map();
export const episodeCache = new Map();

export const state = {
  currentShowsManifest: null,
  currentShow: null,
  currentEpisodesList: [],
  currentEpisode: null,
  currentActiveShow: null,
  currentActiveEpisodeMeta: null,
  currentLangMode: 'bilingual',
  currentBlurMode: 'off',
  loopState: {
    active: false,
    index: -1,
    startSec: 0,
    endSec: 0
  },
  isAutoScrollEnabled: false,
  isScrubbing: false,
  lastActiveTranscriptIndex: -1,
  lastActiveHighlightIndex: -1,
  currentRateIndex: 0,
  currentLoadingShowId: null,
  currentLoadingGuid: null,
  currentRequestSeq: 0,
  userInteractedSeek: false,
  lastProgressWriteTime: 0,
  highlightElements: [],
  transcriptElements: []
};

// Cached DOM Elements
export const elements = {};

export function initElements() {
  elements.audio = document.getElementById('audio-engine');
  elements.playerBar = document.getElementById('audio-player-bar');
  elements.episodeSelect = document.getElementById('episode-select');
  elements.loadingState = document.getElementById('loading-state');
  elements.errorState = document.getElementById('error-state');
  elements.errorMessage = document.getElementById('error-message');
  elements.emptyState = document.getElementById('empty-state');
  elements.emptyShowTitle = document.getElementById('empty-show-title');
  elements.emptyShowDesc = document.getElementById('empty-show-desc');
  elements.emptyRssLink = document.getElementById('empty-rss-link');
  elements.podcastTabs = document.getElementById('podcast-tabs');
  elements.brandShowTitle = document.getElementById('brand-show-title');
  elements.brandLink = document.getElementById('brand-link');
  elements.episodeContent = document.getElementById('episode-content');
  elements.btnRetry = document.getElementById('btn-retry');

  // Archive Drawer
  elements.btnOpenArchive = document.getElementById('issue-archive-btn');
  elements.headerIssueTitle = document.getElementById('header-issue-title');
  elements.headerIssueCount = document.getElementById('header-issue-count');
  elements.archiveBackdrop = document.getElementById('archive-backdrop');
  elements.archiveDrawer = document.getElementById('archive-drawer');
  elements.archiveCloseBtn = document.getElementById('archive-close-btn');
  elements.archiveShowTag = document.getElementById('archive-show-tag');
  elements.archiveShowTitle = document.getElementById('archive-show-title');
  elements.archiveList = document.getElementById('archive-list');
  elements.archiveGrabber = elements.archiveDrawer ? elements.archiveDrawer.querySelector('.archive-grabber') : null;
  elements.archiveHeader = elements.archiveDrawer ? elements.archiveDrawer.querySelector('.archive-header') : null;

  // Mobile Header Controls
  elements.mobileArchiveBtn = document.getElementById('mobile-archive-btn');
  elements.mobileLangBtn = document.getElementById('mobile-lang-btn');
  elements.mobileSearchBtn = document.getElementById('mobile-search-btn');

  // Article Elements
  elements.metaPodcastTitle = document.getElementById('meta-podcast-title');
  elements.metaPubDate = document.getElementById('meta-pub-date');
  elements.metaDuration = document.getElementById('meta-duration');
  elements.metaAudioLink = document.getElementById('meta-audio-link');
  elements.episodeTitle = document.getElementById('episode-title');
  elements.summaryZhText = document.getElementById('summary-zh-text');
  elements.takeawaysCountBadge = document.getElementById('takeaways-count-badge');
  elements.takeawaysList = document.getElementById('takeaways-list');
  elements.highlightsGrid = document.getElementById('highlights-grid');
  elements.highlightsCountBadge = document.getElementById('highlights-count-badge');
  elements.transcriptContainer = document.getElementById('transcript-container');
  elements.transcriptCountBadge = document.getElementById('transcript-count-badge');
  elements.mastheadScaleEnd = document.getElementById('masthead-scale-end');
  elements.mastheadRulerTrack = document.getElementById('masthead-ruler-track');
  elements.mastheadRulerProgress = document.getElementById('masthead-ruler-progress');
  elements.mastheadMicroTicks = document.getElementById('masthead-micro-ticks');
  elements.mastheadChapterTicks = document.getElementById('masthead-chapter-ticks');
  elements.mastheadPlayhead = document.getElementById('masthead-playhead');
  elements.mastheadRulerTooltip = document.getElementById('masthead-ruler-tooltip');

  // Knowledge Cards Elements
  elements.knowledgeSection = document.getElementById('knowledge-section');
  elements.knowledgeTotalBadge = document.getElementById('knowledge-total-badge');
  elements.vocabAccordion = document.getElementById('vocab-accordion');
  elements.vocabCountText = document.getElementById('vocab-count-text');
  elements.vocabGrid = document.getElementById('vocab-grid');
  elements.referencesAccordion = document.getElementById('references-accordion');
  elements.referencesCountText = document.getElementById('references-count-text');
  elements.referencesList = document.getElementById('references-list');

  // Search Elements
  elements.searchBoxWrap = document.getElementById('search-box-wrap');
  elements.searchInput = document.getElementById('search-input');
  elements.searchClearBtn = document.getElementById('search-clear-btn');
  elements.searchStatsBanner = document.getElementById('search-stats-banner');
  elements.searchStatsText = document.getElementById('search-stats-text');
  elements.searchResetLink = document.getElementById('search-reset-link');

  // Player Elements
  elements.btnPlayPause = document.getElementById('btn-play-pause');
  elements.iconPlay = document.getElementById('icon-play');
  elements.iconPause = document.getElementById('icon-pause');
  elements.btnRewind = document.getElementById('btn-rewind');
  elements.btnForward = document.getElementById('btn-forward');
  elements.btnSpeed = document.getElementById('btn-speed');
  elements.btnAutoscroll = document.getElementById('btn-autoscroll');
  elements.seekSlider = document.getElementById('seek-slider');
  elements.timeCurrent = document.getElementById('time-current');
  elements.timeTotal = document.getElementById('time-total');
  elements.playerEpTitle = document.getElementById('player-ep-title');
  elements.playerSpeakerName = document.getElementById('player-speaker-name');
  elements.themeToggleBtn = document.getElementById('theme-toggle-btn');
  elements.iconSun = elements.themeToggleBtn ? elements.themeToggleBtn.querySelector('.icon-sun') : null;
  elements.iconMoon = elements.themeToggleBtn ? elements.themeToggleBtn.querySelector('.icon-moon') : null;

  // Chapter & Resume Elements
  elements.chapterMarkers = document.getElementById('chapter-markers');
  elements.markerTooltip = document.getElementById('marker-tooltip');
  elements.playerChapterBadge = document.getElementById('player-chapter-badge');
  elements.playerChapterTitle = document.getElementById('player-chapter-title');
  elements.resumeBanner = document.getElementById('resume-banner');
  elements.resumeTimeEl = document.getElementById('resume-time');
  elements.btnResumePlay = document.getElementById('btn-resume-play');
  elements.btnResumeRestart = document.getElementById('btn-resume-restart');
  elements.btnResumeClose = document.getElementById('btn-resume-close');

  // Blur & Loop & Shortcuts Elements
  elements.blurSegmentBtns = document.querySelectorAll('.blur-segment-btn');
  elements.loopBanner = document.getElementById('loop-banner');
  elements.loopBannerText = document.getElementById('loop-banner-text');
  elements.btnLoopExit = document.getElementById('btn-loop-exit');
  elements.btnKeyboardShortcuts = document.getElementById('btn-keyboard-shortcuts');
  elements.shortcutsBackdrop = document.getElementById('shortcuts-backdrop');
  elements.shortcutsModal = document.getElementById('shortcuts-modal');
  elements.shortcutsCloseBtn = document.getElementById('shortcuts-close-btn');

  return elements;
}
