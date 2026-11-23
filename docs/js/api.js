/**
 * api.js - Data loading & caching layer
 * Three-tier lazy loading: shows.json -> index.json -> episode JSON
 */

import { showIndexCache, episodeCache } from './state.js';

export async function fetchShows() {
  const response = await fetch('./data/shows.json', { cache: 'no-cache' });
  if (!response.ok) {
    throw new Error(`无法获取节目清单 (HTTP ${response.status} ${response.statusText})`);
  }
  return await response.json();
}

export async function fetchShowIndex(show) {
  if (!show || !show.index_file) {
    throw new Error('无效的播客节目配置');
  }
  if (showIndexCache.has(show.id)) {
    return showIndexCache.get(show.id);
  }
  const fileUrl = `./data/${show.index_file}`;
  const response = await fetch(fileUrl, { cache: 'no-cache' });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`);
  }
  const data = await response.json();
  showIndexCache.set(show.id, data);
  return data;
}

export async function fetchEpisodeDetail(show, epMeta) {
  if (!show || !epMeta || !epMeta.file) {
    throw new Error('无效的单集元数据');
  }
  const cacheKey = `${show.id}::${epMeta.guid}`;
  if (episodeCache.has(cacheKey)) {
    return episodeCache.get(cacheKey);
  }
  const fileUrl = `./data/${show.id}/${epMeta.file}`;
  const response = await fetch(fileUrl, { cache: 'no-cache' });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`);
  }
  const detail = await response.json();
  episodeCache.set(cacheKey, detail);
  return detail;
}
