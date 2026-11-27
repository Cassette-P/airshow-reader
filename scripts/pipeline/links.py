"""Deterministic multi-source direct link resolver & entity disambiguator (Schema v3.2).

Resolves verified direct links for podcast ReferenceItem entries without asking LLMs
to fabricate numeric ID URLs:
- Show Notes first-party Markdown link extraction and matching
- MediaWiki Action API (en/zh Wikipedia) with disambiguation-page filtering & creator/genre verification
- Goodreads book autocomplete API (www.goodreads.com/book/show/...)
- Douban subject suggest API (book/movie.douban.com/subject/<digits>/)
- Apple iTunes Search API (podcasts.apple.com/.../id<digits>) + RSS <channel><link> official podcast site
- Strict URL sanitization (tracking-param stripping), anti-search-page & anti-shortlink redlines
"""

import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests

from pipeline.config import log
from pipeline.models import SOURCE_LABEL_MAP, normalize_reference_links

LINKS_HTTP_HEADERS = {
    "User-Agent": (
        "airshow-reader/3.6 (+https://github.com/cassette-p/airshow-reader) "
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}

WIKI_HTTP_HEADERS = {
    "User-Agent": "airshow-reader/3.6 (https://github.com/cassette-p/airshow-reader; podcast-bilingual-reader)",
    "Accept": "application/json",
}

# Tracking & attribution query parameters stripped during canonicalization
TRACKING_QUERY_KEYS = {
    "spm",
    "ref",
    "ref_",
    "uo",
    "from_search",
    "from_choice",
    "from_home_module",
    "tag",
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "si",
    "igshid",
    "smid",
    "mkt_tok",
    "ck_subscriber_id",
    "linkcode",
    "psc",
    "qid",
    "sr",
    "dib",
    "dib_tag",
    "keywords",
    "ac",
    "rank",
    "from_srp",
}

# Shortlinks, ad-tracking redirectors, and sponsor networks strictly prohibited
BLOCKED_DOMAINS = {
    "bit.ly",
    "tinyurl.com",
    "amzn.to",
    "t.co",
    "ow.ly",
    "buff.ly",
    "goo.gl",
    "rb.gy",
    "is.gd",
    "podtrac.com",
    "dts.podtrac.com",
    "chartable.com",
    "chtbl.com",
    "link.chtbl.com",
    "pdst.fm",
    "pscrb.fm",
    "arttrk.com",
    "swap.fm",
    "mgln.ai",
    "pfx.vpixl.com",
    "verifi.podscribe.com",
    "linksynergy.com",
    "doubleclick.net",
}

# Podcast RSS hosting domains that should never be used as a human-facing "播客官网"
RSS_HOSTING_DOMAINS = {
    "feeds.buzzsprout.com",
    "rss.art19.com",
    "feeds.megaphone.fm",
    "feeds.simplecast.com",
    "feeds.transistor.fm",
    "feeds.acast.com",
    "feeds.libsyn.com",
    "feeds.fireside.fm",
    "feeds.captivate.fm",
    "feeds.podbean.com",
    "anchor.fm",
    "omnycontent.com",
    "feeds.npr.org",
    "feeds.pacific-content.com",
}

# Search result URL patterns strictly prohibited
SEARCH_URL_PATTERNS = [
    re.compile(r"/w/index\.php\?[^#]*\bsearch=", re.IGNORECASE),
    re.compile(r"/wiki/Special:Search", re.IGNORECASE),
    re.compile(r"://search\.douban\.com(?:/|$)", re.IGNORECASE),
    re.compile(r"douban\.com/subject_search", re.IGNORECASE),
    re.compile(r"goodreads\.com/search(?:/|\?|$)", re.IGNORECASE),
    re.compile(r"://(?:www\.)?google\.[a-z.]+/search(?:/|\?|$)", re.IGNORECASE),
    re.compile(r"://(?:www\.)?bing\.com/search(?:/|\?|$)", re.IGNORECASE),
    re.compile(r"://(?:www\.)?duckduckgo\.com/\?[^#]*\bq=", re.IGNORECASE),
    re.compile(r"://search\.yahoo\.com(?:/|$)", re.IGNORECASE),
]

# Platform-specific canonical direct URL regexes
PLATFORM_URL_PATTERNS = {
    "wikipedia": re.compile(
        r"^https?://(?:en|zh)\.wikipedia\.org/wiki/(?!Special:)[^\s?]+$",
        re.IGNORECASE,
    ),
    "douban": re.compile(
        r"^https?://(?:book|movie|music)\.douban\.com/subject/\d+/?$",
        re.IGNORECASE,
    ),
    "goodreads": re.compile(
        r"^https?://(?:www\.)?goodreads\.com/book/show/\d+(?:[.-][^\s/?#]+)?/?$",
        re.IGNORECASE,
    ),
    "apple_podcasts": re.compile(
        r"^https?://podcasts\.apple\.com/(?:[a-z]{2}/)?podcast/(?:[^/\s]+/)?id\d+(?:\?i=\d+)?$",
        re.IGNORECASE,
    ),
}

MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "by", "at", "with",
    "from", "as", "is", "are", "was", "were", "be", "about", "how", "what", "why",
    "who", "its", "their", "our", "your", "this", "that", "new", "episode", "podcast",
    "show", "book", "article", "report", "series", "story", "substack", "edition",
}


def is_blocked_domain(host: str) -> bool:
    """Return True if host is a prohibited shortlink or ad-tracking redirector."""
    h = (host or "").lower().strip().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    if h in BLOCKED_DOMAINS:
        return True
    for blocked in BLOCKED_DOMAINS:
        if h.endswith("." + blocked):
            return True
    return False


def is_search_page_url(url: str) -> bool:
    """Return True if URL points to a search result page."""
    if not url:
        return False
    return any(pat.search(url) for pat in SEARCH_URL_PATTERNS)


def is_bare_rss_url(url: str) -> bool:
    """Return True if URL looks like a raw RSS/XML feed rather than a human-facing site."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host in RSS_HOSTING_DOMAINS:
        return True
    if path.endswith((".rss", ".xml", "/rss", "/feed", "/feed/")):
        return True
    if "/rss/" in path or "/feeds/" in path:
        return True
    return False


def sanitize_url(url: str, *, allow_rss: bool = False) -> str:
    """Validate protocol, strip tracking params, block shortlinks and search pages."""
    if not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw or len(raw) > 1024 or any(ch.isspace() for ch in raw):
        return ""
    if not (raw.startswith("https://") or raw.startswith("http://")):
        return ""

    parsed = urlparse(raw)
    if not parsed.netloc:
        return ""

    host_lower = parsed.netloc.lower()
    if is_blocked_domain(host_lower):
        return ""
    if is_search_page_url(raw):
        return ""
    if not allow_rss and is_bare_rss_url(raw):
        return ""

    # Strip tracking query parameters while keeping meaningful ones (e.g. Apple Podcasts ?i=<ep_id>)
    kept_params = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=False):
        kl = k.lower()
        if kl.startswith("utm_") or kl in TRACKING_QUERY_KEYS:
            continue
        kept_params.append((k, v))

    scheme = parsed.scheme.lower()
    # Upgrade known canonical platforms to https
    if any(
        host_lower.endswith(dom)
        for dom in ("wikipedia.org", "douban.com", "goodreads.com", "podcasts.apple.com")
    ):
        scheme = "https"

    # Strip query & fragment completely on wikipedia/douban/goodreads canonical pages
    clean_query = urlencode(kept_params)
    clean_fragment = parsed.fragment
    if host_lower.endswith(("douban.com", "goodreads.com")):
        clean_query = ""
        clean_fragment = ""
    elif host_lower.endswith("wikipedia.org"):
        clean_query = ""
    elif host_lower == "podcasts.apple.com":
        # Keep only episode parameter 'i' if digits
        ep_params = [(k, v) for k, v in kept_params if k == "i" and v.isdigit()]
        clean_query = urlencode(ep_params)
        clean_fragment = ""

    clean_path = parsed.path
    if host_lower.endswith("douban.com") and re.match(r"^/subject/\d+$", clean_path):
        clean_path += "/"

    rebuilt = urlunparse((scheme, parsed.netloc, clean_path, "", clean_query, clean_fragment))
    if len(rebuilt) > 1024 or is_search_page_url(rebuilt):
        return ""
    return rebuilt


def validate_platform_url(source: str, url: str) -> bool:
    """Validate that a URL conforms to platform-specific direct path rules."""
    if not url or is_search_page_url(url):
        return False
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return False
    if is_blocked_domain(parsed.netloc):
        return False
    if source == "podcast_site" and is_bare_rss_url(url):
        return False
    pat = PLATFORM_URL_PATTERNS.get(source)
    if pat is not None:
        return bool(pat.match(url))
    return True


def extract_show_notes_links(show_notes: str) -> list[dict[str, str]]:
    """Extract sanitized (text, url) links from Markdown-formatted Show Notes."""
    if not show_notes:
        return []
    found: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for anchor, raw_url in MARKDOWN_LINK_RE.findall(show_notes):
        clean = sanitize_url(raw_url)
        if not clean or clean in seen_urls:
            continue
        seen_urls.add(clean)
        found.append({"text": anchor.strip(), "url": clean})
    return found


def _split_title_variants(title: str) -> tuple[str, str, str]:
    """Return (primary_title, parenthetical_text, main_title_without_subtitle)."""
    t = (title or "").strip()
    paren_match = re.search(r"[（(]([^()（）]{2,80})[）)]\s*$", t)
    paren_text = paren_match.group(1).strip() if paren_match else ""
    primary = t[: paren_match.start()].strip() if paren_match else t
    main_no_sub = re.split(r"[:：]", primary, maxsplit=1)[0].strip()
    return primary, paren_text, main_no_sub


def _extract_creator_surnames(creator: str) -> list[str]:
    """Extract normalized meaningful creator surnames/tokens for disambiguation."""
    if not creator:
        return []
    cleaned = re.sub(r"\([^)]*\)", " ", creator)
    cleaned = re.sub(r"等$", "", cleaned).strip()
    parts = re.split(r"[,&/]|\band\b", cleaned, flags=re.IGNORECASE)
    surnames: list[str] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if CJK_RE.search(p):
            cjk_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", p)
            surnames.extend(cjk_tokens)
        else:
            words = re.findall(r"[A-Za-z][A-Za-z'-]{1,}", p)
            if words:
                last = words[-1].lower()
                if last not in STOPWORDS and len(last) >= 3:
                    surnames.append(last)
    return list(dict.fromkeys(surnames))


def _tokenize(text: str) -> set[str]:
    """Extract lowercase alphanumeric/CJK tokens excluding stopwords."""
    if not text:
        return set()
    tokens = set()
    for w in re.findall(r"[A-Za-z0-9]{2,}", text.lower()):
        if w not in STOPWORDS:
            tokens.add(w)
    for cjk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.add(cjk)
    return tokens


def classify_reference_subtype(ref: dict[str, Any]) -> str:
    """Classify a ReferenceItem into a routing subtype per SPEC §4.3.1."""
    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    category = str(ref.get("category") or "other").lower().strip()
    note_zh = str(ref.get("note_zh") or "")
    combined = f"{title} {creator} {note_zh}".lower()

    if category == "book":
        primary, _, _ = _split_title_variants(title)
        if CJK_RE.search(primary):
            return "book_zh"
        return "book_foreign"

    if category == "media":
        if any(kw in combined for kw in ("纪录片", "电影", "剧集", "影剧", "documentary", "film", "movie")):
            # Exclude song from a film (e.g. "If I Were King of the Forest" from The Wizard of Oz)
            if any(kw in combined for kw in ("插曲", "配乐", "歌曲", "song")):
                return "article_or_other"
            return "film_doc"
        if any(
            kw in combined
            for kw in (
                "播客", "podcast", "单集", "广播节目", "radio program", "all things considered",
                "ezra klein show", "cheeky pint", "high capacity", "agent layla",
            )
        ):
            return "podcast"
        return "article_or_other"

    return "article_or_other"


def _classify_url_source(url: str, subtype: str, category: str) -> str:
    """Infer ReferenceLink source enum from a verified URL and item subtype."""
    host = urlparse(url).netloc.lower()
    if host.endswith("wikipedia.org") and validate_platform_url("wikipedia", url):
        return "wikipedia"
    if host.endswith("douban.com") and validate_platform_url("douban", url):
        return "douban"
    if host.endswith("goodreads.com") and validate_platform_url("goodreads", url):
        return "goodreads"
    if host == "podcasts.apple.com" and validate_platform_url("apple_podcasts", url):
        return "apple_podcasts"
    if subtype == "podcast":
        return "podcast_site"
    if category == "article":
        path = urlparse(url).path.strip("/")
        return "article" if path else "official"
    return "official"


def match_show_notes_links(
    ref: dict[str, Any],
    show_notes_links: list[dict[str, str]],
    subtype: str,
) -> list[dict[str, str]]:
    """Match relevant first-party links extracted from Show Notes against a ReferenceItem."""
    if not show_notes_links:
        return []

    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    category = str(ref.get("category") or "other")
    primary, paren, main_no_sub = _split_title_variants(title)

    title_tokens = _tokenize(primary) | _tokenize(main_no_sub) | _tokenize(paren)
    creator_tokens = set(_extract_creator_surnames(creator))
    if not title_tokens:
        return []

    matched: list[dict[str, str]] = []
    for entry in show_notes_links:
        url = entry["url"]
        anchor = entry["text"]
        parsed = urlparse(url)
        url_slug_text = f"{parsed.netloc} {parsed.path}"
        cand_tokens = _tokenize(anchor) | _tokenize(url_slug_text)

        overlap = title_tokens & cand_tokens
        if not overlap:
            continue

        # Require strong token overlap (either >= 2 tokens, or full match on short 1-token titles with domain/anchor match)
        ratio = len(overlap) / max(1, len(title_tokens))
        is_exact_phrase = main_no_sub.lower() in anchor.lower() or (
            len(main_no_sub) >= 5 and re.sub(r"[^a-z0-9]", "", main_no_sub.lower()) in re.sub(r"[^a-z0-9]", "", url_slug_text.lower())
        )

        # Guard short ambiguous 1-word book titles (Augustus, Home, Stalingrad, Whistler)
        if len(title_tokens) == 1 and subtype.startswith("book"):
            has_creator = bool(creator_tokens & cand_tokens)
            host = parsed.netloc.lower()
            is_book_host = host.endswith(("goodreads.com", "douban.com", "wikipedia.org"))
            if not (has_creator or (is_exact_phrase and is_book_host)):
                continue
        elif not (is_exact_phrase or len(overlap) >= 2 or (len(title_tokens) == 1 and ratio >= 1.0)):
            continue

        # Ignore generic social media / sponsor / podcast host homepage links unless explicitly referencing that site
        host = parsed.netloc.lower()
        if any(host.endswith(soc) for soc in ("twitter.com", "x.com", "instagram.com", "facebook.com", "linkedin.com", "patreon.com")):
            continue

        source = _classify_url_source(url, subtype, category)
        if validate_platform_url(source, url):
            matched.append({"source": source, "label": SOURCE_LABEL_MAP[source], "url": url})

    return matched


def _wiki_candidate_matches(
    ref: dict[str, Any],
    subtype: str,
    page_title: str,
    extract: str,
    is_disambig: bool,
) -> bool:
    """Strict disambiguation check for a Wikipedia page candidate."""
    if is_disambig:
        return False

    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    primary, paren, main_no_sub = _split_title_variants(title)

    pt_lower = page_title.lower()
    clean_pt = re.sub(r"\s*\([^)]*\)$", "", pt_lower).strip()
    ex_lower = (extract or "").lower()
    combined_wiki = f"{pt_lower} {ex_lower}"

    primary_tokens = _tokenize(main_no_sub)
    paren_tokens = _tokenize(paren)
    page_tokens = _tokenize(clean_pt)

    surnames = _extract_creator_surnames(creator)
    has_creator_match = any(sn in combined_wiki for sn in surnames) if surnames else False

    if subtype in ("book_foreign", "book_zh"):
        # Require strong title match on the Wikipedia page title itself (prevents list pages like Writers_on_business_in_China)
        title_ok = (
            clean_pt == main_no_sub.lower()
            or main_no_sub.lower() in clean_pt
            or (paren and not CJK_RE.search(paren) and paren.lower() in clean_pt)
            or (
                bool(primary_tokens)
                and len(primary_tokens & page_tokens) / len(primary_tokens) >= 0.75
            )
        )
        if not title_ok:
            return False
        book_markers = (
            "novel", "book", "non-fiction", "nonfiction", "memoir", "monograph",
            "written by", "authored by", "published in", "小说", "著作", "书籍", "一书", "作者",
        )
        has_book_genre = any(m in combined_wiki for m in book_markers)
        # For books, ALWAYS require book genre marker; if creator is known, also require creator match
        # (prevents Augustus -> Roman Emperor, Home -> dwelling, Stalingrad -> Battle of Stalingrad)
        if not has_book_genre:
            return False
        if surnames and not has_creator_match:
            return False
        return True

    if subtype == "film_doc":
        if not (
            clean_pt == main_no_sub.lower()
            or main_no_sub.lower() in clean_pt
            or (bool(primary_tokens) and len(primary_tokens & page_tokens) / len(primary_tokens) >= 0.75)
        ):
            return False
        film_markers = ("documentary", "film", "movie", "television", "series", "directed by", "纪录片", "电影", "剧集")
        if not any(m in combined_wiki for m in film_markers):
            return False
        if surnames and not has_creator_match:
            if main_no_sub.lower() != clean_pt:
                return False
        return True

    if subtype == "podcast":
        base_show = re.sub(r"\s*\([^)]*episode[^)]*\)\s*", "", primary, flags=re.IGNORECASE).strip()
        base_lower = base_show.lower()
        base_tokens = _tokenize(base_show)
        # Prevent matching a host's personal biography page when ref is a named show (e.g. "The Ezra Klein Show" -> "Ezra Klein")
        if ("show" in base_lower or "podcast" in base_lower) and ("show" not in pt_lower and "podcast" not in pt_lower):
            return False
        pod_markers = ("podcast", "radio", "broadcast", "npr", "cbc", "hosted by", "播客", "广播")
        if not any(m in combined_wiki for m in pod_markers):
            return False
        if base_lower == clean_pt or base_lower in clean_pt:
            return True
        if base_tokens and len(base_tokens & page_tokens) / len(base_tokens) >= 0.75:
            return True
        return False

    # Articles / tools / laws / speeches / publications:
    # Reject personal Substack/newsletter refs from matching the author's person bio
    if "substack" in title.lower():
        return False
    # Exact page title match (or parenthetical alias match)
    if clean_pt == main_no_sub.lower() or (paren and clean_pt == paren.lower()):
        return True
    # High bidirectional token overlap (prevents Simple Flying -> Flying car, Canada -> South Korea, 2020 Act -> 2016 Act)
    if primary_tokens and page_tokens:
        recall = len(primary_tokens & page_tokens) / len(primary_tokens)
        precision = len(primary_tokens & page_tokens) / len(page_tokens)
        if recall >= 0.8 and precision >= 0.6:
            return True
        # Also allow parenthetical full name match (e.g. "PCL-R (Psychopathy Checklist-Revised)" -> "Psychopathy Checklist")
        if paren_tokens and len(paren_tokens) >= 2:
            p_rec = len(paren_tokens & page_tokens) / len(paren_tokens)
            p_pre = len(paren_tokens & page_tokens) / len(page_tokens)
            if p_rec >= 0.65 and p_pre >= 0.75:
                return True
    return False


def _query_wikipedia_lang(
    lang: str,
    query: str,
    ref: dict[str, Any],
    subtype: str,
    timeout: int = 10,
) -> Optional[str]:
    """Query MediaWiki Action API for a given language ('en' or 'zh') with disambiguation checks."""
    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        search_resp = requests.get(
            api_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": 5,
                "format": "json",
            },
            headers=WIKI_HTTP_HEADERS,
            timeout=timeout,
        )
        if search_resp.status_code != 200:
            return None
        hits = search_resp.json().get("query", {}).get("search", [])
        if not hits:
            return None

        titles = [h["title"] for h in hits if h.get("title")]
        if not titles:
            return None

        detail_resp = requests.get(
            api_url,
            params={
                "action": "query",
                "titles": "|".join(titles),
                "prop": "extracts|pageprops|info",
                "exintro": 1,
                "explaintext": 1,
                "exchars": 600,
                "inprop": "url",
                "ppprop": "disambiguation",
                "redirects": 1,
                "format": "json",
            },
            headers=WIKI_HTTP_HEADERS,
            timeout=timeout,
        )
        if detail_resp.status_code != 200:
            return None
        pages = detail_resp.json().get("query", {}).get("pages", {})

        # Preserve search ranking order
        by_title = {}
        for p in pages.values():
            if isinstance(p, dict) and p.get("title") and not p.get("missing"):
                by_title[p["title"]] = p

        ordered_pages = []
        for t in titles:
            if t in by_title:
                ordered_pages.append(by_title[t])
        for p in by_title.values():
            if p not in ordered_pages:
                ordered_pages.append(p)

        for page in ordered_pages:
            page_title = page.get("title", "")
            full_url = sanitize_url(page.get("fullurl", ""))
            if not full_url or not validate_platform_url("wikipedia", full_url):
                continue
            pageprops = page.get("pageprops", {}) or {}
            is_disambig = "disambiguation" in pageprops
            extract = page.get("extract", "") or ""
            if _wiki_candidate_matches(ref, subtype, page_title, extract, is_disambig):
                return full_url
    except Exception as e:
        log("WARN", f"Wikipedia ({lang}) lookup failed for '{query}': {e}")
    return None


def resolve_wikipedia_link(ref: dict[str, Any], subtype: str, timeout: int = 10) -> Optional[dict[str, str]]:
    """Resolve verified Wikipedia direct link with language routing and entity disambiguation."""
    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    primary, paren, main_no_sub = _split_title_variants(title)
    surnames = _extract_creator_surnames(creator)
    surname_hint = surnames[0] if surnames else ""

    queries_en: list[str] = []
    queries_zh: list[str] = []

    if subtype == "book_zh":
        queries_zh.append(f"{main_no_sub} {surname_hint}".strip())
        if main_no_sub != primary:
            queries_zh.append(main_no_sub)
        if paren:
            queries_en.append(f"{paren} {surname_hint}".strip())
    elif subtype == "book_foreign":
        if surname_hint:
            queries_en.append(f"{main_no_sub} {surname_hint} book")
            queries_en.append(f"{main_no_sub} {surname_hint}")
        else:
            queries_en.append(f"{main_no_sub} book")
    elif subtype == "film_doc":
        queries_en.append(f"{main_no_sub} documentary film {surname_hint}".strip())
        queries_en.append(f"{main_no_sub} film")
    elif subtype == "podcast":
        # Strip "(Episode with ...)" to match the podcast show itself
        base_show = re.sub(r"\s*\([^)]*episode[^)]*\)\s*", "", primary, flags=re.IGNORECASE).strip()
        queries_en.append(f"{base_show} podcast")
        queries_en.append(base_show)
    else:
        queries_en.append(f"{main_no_sub} {surname_hint}".strip())
        if paren and not CJK_RE.search(paren):
            queries_en.append(paren)
        elif paren and CJK_RE.search(paren):
            queries_zh.append(paren)

    # Deduplicate queries
    queries_en = list(dict.fromkeys(q for q in queries_en if q))
    queries_zh = list(dict.fromkeys(q for q in queries_zh if q))

    lang_order = [("zh", queries_zh), ("en", queries_en)] if subtype == "book_zh" else [("en", queries_en), ("zh", queries_zh)]
    for lang, q_list in lang_order:
        for q in q_list:
            url = _query_wikipedia_lang(lang, q, ref, subtype, timeout=timeout)
            if url:
                return {"source": "wikipedia", "label": SOURCE_LABEL_MAP["wikipedia"], "url": url}
    return None


def resolve_goodreads_link(ref: dict[str, Any], timeout: int = 10) -> Optional[dict[str, str]]:
    """Resolve verified Goodreads direct book link via book/auto_complete JSON endpoint."""
    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    primary, paren, main_no_sub = _split_title_variants(title)
    surnames = _extract_creator_surnames(creator)
    title_tokens = _tokenize(main_no_sub) or _tokenize(primary)
    if not title_tokens:
        return None

    queries = []
    if surnames:
        queries.append(f"{main_no_sub} {surnames[0]}")
    queries.append(main_no_sub)
    if paren and not CJK_RE.search(paren):
        queries.append(paren)

    for q in dict.fromkeys(queries):
        try:
            resp = requests.get(
                "https://www.goodreads.com/book/auto_complete",
                params={"format": "json", "q": q},
                headers=LINKS_HTTP_HEADERS,
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue
            items = resp.json()
            if not isinstance(items, list):
                continue

            for cand in items:
                if not isinstance(cand, dict):
                    continue
                cand_title = str(cand.get("bookTitleBare") or cand.get("title") or "")
                author_obj = cand.get("author") or {}
                cand_author = str(author_obj.get("name") or "") if isinstance(author_obj, dict) else ""
                book_path = str(cand.get("bookUrl") or "").strip()
                if not book_path:
                    continue

                # Verify title and creator match
                cand_title_tokens = _tokenize(cand_title)
                overlap = title_tokens & cand_title_tokens
                if not overlap:
                    continue
                if len(title_tokens) > 1 and len(overlap) / len(title_tokens) < 0.6:
                    continue
                if len(title_tokens) == 1 and main_no_sub.lower() not in cand_title.lower():
                    continue
                if surnames:
                    cand_author_lower = cand_author.lower()
                    if not any(sn in cand_author_lower for sn in surnames):
                        continue

                full_url = f"https://www.goodreads.com{book_path}" if book_path.startswith("/") else book_path
                clean_url = sanitize_url(full_url)
                if clean_url and validate_platform_url("goodreads", clean_url):
                    return {"source": "goodreads", "label": SOURCE_LABEL_MAP["goodreads"], "url": clean_url}
        except Exception as e:
            log("WARN", f"Goodreads lookup failed for '{q}': {e}")
            break
    return None


def resolve_douban_link(ref: dict[str, Any], subtype: str, timeout: int = 10) -> Optional[dict[str, str]]:
    """Resolve verified Douban direct subject link via j/subject_suggest (book or movie)."""
    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    primary, paren, main_no_sub = _split_title_variants(title)
    surnames = _extract_creator_surnames(creator)

    is_movie = subtype == "film_doc"
    endpoint = (
        "https://movie.douban.com/j/subject_suggest"
        if is_movie
        else "https://book.douban.com/j/subject_suggest"
    )

    queries = []
    if CJK_RE.search(primary):
        queries.append(main_no_sub)
        if primary != main_no_sub:
            queries.append(primary)
    if paren and CJK_RE.search(paren):
        queries.append(paren)
    queries.append(main_no_sub)

    for q in dict.fromkeys(x for x in queries if x):
        try:
            resp = requests.get(
                endpoint,
                params={"q": q},
                headers={
                    **LINKS_HTTP_HEADERS,
                    "Referer": "https://movie.douban.com/" if is_movie else "https://book.douban.com/",
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                log("WARN", f"Douban suggest returned HTTP {resp.status_code} for '{q}'; skipping gracefully.")
                return None
            items = resp.json()
            if not isinstance(items, list):
                continue

            for cand in items:
                if not isinstance(cand, dict):
                    continue
                cand_url = sanitize_url(str(cand.get("url") or ""))
                if not cand_url or not validate_platform_url("douban", cand_url):
                    continue
                cand_title = str(cand.get("title") or "")
                cand_sub = str(cand.get("sub_title") or "")
                cand_author = str(cand.get("author_name") or "")
                cand_text = f"{cand_title} {cand_sub}".lower()

                # Check title match
                title_ok = (
                    (paren and paren.lower() in cand_text)
                    or main_no_sub.lower() in cand_text
                    or (CJK_RE.search(main_no_sub) and main_no_sub in cand_title)
                )
                if not title_ok:
                    continue

                # Check creator match when author_name is returned by Douban
                if not is_movie and surnames and cand_author:
                    cand_author_l = cand_author.lower()
                    # Author might be translated into Chinese on Douban, so only reject if Latin author mismatches
                    if not CJK_RE.search(cand_author) and not any(sn in cand_author_l for sn in surnames):
                        continue

                return {"source": "douban", "label": SOURCE_LABEL_MAP["douban"], "url": cand_url}
        except Exception as e:
            log("WARN", f"Douban lookup failed for '{q}': {e}")
            return None
    return None


def _extract_feed_channel_link(feed_url: str, timeout: int = 10) -> Optional[str]:
    """Fetch RSS feed XML and extract clean human-facing <channel><link> official website."""
    if not feed_url:
        return None
    try:
        resp = requests.get(feed_url, headers=LINKS_HTTP_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            return None
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return None
        link_el = channel.find("link")
        if link_el is None or not link_el.text:
            return None
        clean = sanitize_url(link_el.text.strip())
        if clean and validate_platform_url("podcast_site", clean):
            return clean
    except Exception:
        return None
    return None


def resolve_podcast_links(ref: dict[str, Any], timeout: int = 10) -> list[dict[str, str]]:
    """Resolve Apple Podcasts direct link and official podcast website via Apple iTunes Search API."""
    title = str(ref.get("title") or "")
    creator = str(ref.get("creator") or "")
    primary, _, main_no_sub = _split_title_variants(title)
    surnames = _extract_creator_surnames(creator)
    base_show = re.sub(r"\s*\([^)]*episode[^)]*\)\s*", "", primary, flags=re.IGNORECASE).strip()
    show_tokens = _tokenize(base_show)
    if not show_tokens:
        return []

    links: list[dict[str, str]] = []
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"media": "podcast", "entity": "podcast", "limit": 6, "term": base_show},
            headers=LINKS_HTTP_HEADERS,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return links
        results = resp.json().get("results", [])
        best = None
        for item in results:
            if not isinstance(item, dict):
                continue
            cname = str(item.get("collectionName") or "")
            aname = str(item.get("artistName") or "")
            c_tokens = _tokenize(cname)
            overlap = show_tokens & c_tokens
            if not overlap:
                continue
            ratio = len(overlap) / max(1, len(show_tokens))
            if base_show.lower() in cname.lower() or ratio >= 0.7:
                meta_lower = f"{cname} {aname}".lower()
                if surnames:
                    if any(sn in meta_lower for sn in surnames):
                        best = item
                        break
                    # When creator is explicitly known (e.g. NPR, CBC, Kyle Chan), reject unrelated same-name podcasts
                    continue
                if best is None:
                    best = item

        if best:
            apple_url = sanitize_url(str(best.get("collectionViewUrl") or ""))
            if apple_url and validate_platform_url("apple_podcasts", apple_url):
                links.append({
                    "source": "apple_podcasts",
                    "label": SOURCE_LABEL_MAP["apple_podcasts"],
                    "url": apple_url,
                })
            feed_url = str(best.get("feedUrl") or "").strip()
            site_url = _extract_feed_channel_link(feed_url, timeout=timeout)
            if site_url:
                links.append({
                    "source": "podcast_site",
                    "label": SOURCE_LABEL_MAP["podcast_site"],
                    "url": site_url,
                })
    except Exception as e:
        log("WARN", f"iTunes podcast lookup failed for '{base_show}': {e}")

    return links


def resolve_single_reference_links(
    ref: dict[str, Any],
    show_notes_links: Optional[list[dict[str, str]]] = None,
    enable_network: bool = True,
    timeout: int = 10,
) -> list[dict[str, str]]:
    """Resolve verified direct links for a single ReferenceItem according to SPEC §4.3.1."""
    subtype = classify_reference_subtype(ref)
    collected: list[dict[str, str]] = []

    # 1. Always try first-party Show Notes links first
    if show_notes_links:
        collected.extend(match_show_notes_links(ref, show_notes_links, subtype))

    if not enable_network:
        return normalize_reference_links(collected)

    existing_sources = {item["source"] for item in collected}

    # 2. Platform routing matrix
    if subtype == "film_doc":
        if "wikipedia" not in existing_sources:
            wiki = resolve_wikipedia_link(ref, subtype, timeout=timeout)
            if wiki:
                collected.append(wiki)
        if "douban" not in existing_sources:
            db = resolve_douban_link(ref, subtype, timeout=timeout)
            if db:
                collected.append(db)

    elif subtype == "book_zh":
        if "wikipedia" not in existing_sources:
            wiki = resolve_wikipedia_link(ref, subtype, timeout=timeout)
            if wiki:
                collected.append(wiki)
        if "douban" not in existing_sources:
            db = resolve_douban_link(ref, subtype, timeout=timeout)
            if db:
                collected.append(db)

    elif subtype == "book_foreign":
        if "wikipedia" not in existing_sources:
            wiki = resolve_wikipedia_link(ref, subtype, timeout=timeout)
            if wiki:
                collected.append(wiki)
        if "goodreads" not in existing_sources:
            gr = resolve_goodreads_link(ref, timeout=timeout)
            if gr:
                collected.append(gr)
        # Also check Douban when Chinese title is indicated in parentheses or note, or well-known translated book
        _, paren, _ = _split_title_variants(str(ref.get("title") or ""))
        if "douban" not in existing_sources and paren and CJK_RE.search(paren):
            db = resolve_douban_link(ref, subtype, timeout=timeout)
            if db:
                collected.append(db)

    elif subtype == "podcast":
        if "apple_podcasts" not in existing_sources or "podcast_site" not in existing_sources:
            pod_links = resolve_podcast_links(ref, timeout=timeout)
            for pl in pod_links:
                if pl["source"] not in existing_sources:
                    collected.append(pl)
                    existing_sources.add(pl["source"])
        if "wikipedia" not in existing_sources:
            wiki = resolve_wikipedia_link(ref, subtype, timeout=timeout)
            if wiki:
                collected.append(wiki)

    else:
        # article / newsletter / report / industry tool / law / speech
        if "wikipedia" not in existing_sources:
            wiki = resolve_wikipedia_link(ref, subtype, timeout=timeout)
            if wiki:
                collected.append(wiki)

    # Final validation gate before normalization
    validated = [
        item for item in collected
        if validate_platform_url(item.get("source", ""), item.get("url", ""))
    ]
    return normalize_reference_links(validated, max_links=4)


def resolve_references_links(
    references: list[dict[str, Any]],
    show_notes: str = "",
    show_id: str = "",
    enable_network: bool = True,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """Resolve and attach verified `links` arrays to all ReferenceItem dicts without mutating other fields."""
    if not references:
        return []

    show_notes_links = extract_show_notes_links(show_notes)
    if show_notes_links:
        log("LINKS", f"Extracted {len(show_notes_links)} clean link(s) from Show Notes.", show_id=show_id)

    resolved_list: list[dict[str, Any]] = []
    total_links = 0
    for ref in references:
        item = dict(ref)
        try:
            links = resolve_single_reference_links(
                item,
                show_notes_links=show_notes_links,
                enable_network=enable_network,
                timeout=timeout,
            )
        except Exception as e:
            log("WARN", f"Link resolution error for '{item.get('title', '')}': {e}", show_id=show_id)
            links = []
        item["links"] = links
        total_links += len(links)
        resolved_list.append(item)

    log(
        "LINKS",
        f"Resolved {total_links} verified link(s) across {len(resolved_list)} reference(s).",
        show_id=show_id,
    )
    return resolved_list
