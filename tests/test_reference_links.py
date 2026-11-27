"""Unit tests for M12 Schema v3.2 Reference Multi-Source Direct Links & Channel B upgrade.

Covers:
1. Channel B default model pool upgrade to gemini-4-hydrogen
2. Show Notes HTML -> Markdown hyperlink preservation & sponsor/shortlink stripping (rss.py & hooks_tal.py)
3. URL canonicalization (tracking param stripping, Apple Podcasts ?i= preservation, anti-search-page, anti-shortlink)
4. Multi-type routing & entity disambiguation (Augustus, Home, Stalingrad, Wikipedia disambiguation pages)
5. ReferenceLink normalization, deduplication, canonical label mapping, fixed priority sort, max 4 cap
6. --enrich-links idempotency, --force-links, --dry-run, byte-level preservation of non-links fields & index.json
7. validate_data.py v3.2 link contract checks (strict missing-links WARN, bad labels, duplicates, search/shortlink errors)
"""

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pipeline.config import DEFAULT_CH_B_POOL
from pipeline.hooks_tal import fetch_tal_episode_page
from pipeline.links import (
    _wiki_candidate_matches,
    classify_reference_subtype,
    extract_show_notes_links,
    is_search_page_url,
    match_show_notes_links,
    resolve_single_reference_links,
    sanitize_url,
    validate_platform_url,
)
from pipeline.models import (
    ReferenceItem,
    ReferenceLink,
    clamp_and_sort_references,
    normalize_reference_links,
)
from pipeline.rss import get_entry_show_notes, html_to_markdown_show_notes
from pipeline.runner import run_enrich_links_pipeline
import validate_data


class TestChannelBUpgrade(unittest.TestCase):
    def test_ch_b_pool_defaults_to_gemini_4_hydrogen(self):
        self.assertEqual(
            DEFAULT_CH_B_POOL,
            ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
        )


class TestUrlSanitizationAndShowNotes(unittest.TestCase):
    def test_tracking_params_stripped_and_apple_episode_param_preserved(self):
        self.assertEqual(
            sanitize_url("https://www.newyorker.com/magazine/2026/test?utm_source=rss&utm_medium=feed&spm=123"),
            "https://www.newyorker.com/magazine/2026/test",
        )
        self.assertEqual(
            sanitize_url("https://book.douban.com/subject/26365483?from_search=1&ref=top"),
            "https://book.douban.com/subject/26365483/",
        )
        self.assertEqual(
            sanitize_url("https://www.goodreads.com/book/show/55782468-flying-blind?from_search=true&qid=abc"),
            "https://www.goodreads.com/book/show/55782468-flying-blind",
        )
        self.assertEqual(
            sanitize_url("https://podcasts.apple.com/us/podcast/the-ezra-klein-show/id1548604447?i=1000678901234&uo=4"),
            "https://podcasts.apple.com/us/podcast/the-ezra-klein-show/id1548604447?i=1000678901234",
        )

    def test_search_pages_and_shortlinks_rejected(self):
        bad_urls = [
            "https://search.douban.com/book/subject_search?search_text=Augustus",
            "https://book.douban.com/subject_search?search_text=test",
            "https://www.goodreads.com/search?q=Home",
            "https://en.wikipedia.org/w/index.php?search=Stalingrad",
            "https://en.wikipedia.org/wiki/Special:Search?search=test",
            "https://www.google.com/search?q=foo",
            "https://bit.ly/3abcXYZ",
            "https://amzn.to/4xyz123",
            "https://dts.podtrac.com/redirect.mp3/example.com/a.mp3",
            "https://feeds.buzzsprout.com/2329718.rss",
        ]
        for u in bad_urls:
            self.assertEqual(sanitize_url(u), "", f"Expected rejected URL: {u}")

    def test_html_show_notes_preserves_valid_links_and_strips_sponsor_shortlinks(self):
        raw_html = (
            '<p>Read <a href="https://theaircurrent.com/story/?utm_source=pod">The Air Current</a> '
            'and sponsor <a href="https://bit.ly/promo123">Sponsor Deal</a>.</p>'
        )
        md = html_to_markdown_show_notes(raw_html)
        self.assertIn("[The Air Current](https://theaircurrent.com/story/)", md)
        self.assertIn("Sponsor Deal", md)
        self.assertNotIn("bit.ly", md)

        extracted = extract_show_notes_links(md)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["url"], "https://theaircurrent.com/story/")

    def test_rss_entry_show_notes_preserves_links(self):
        entry = {
            "summary": '<p>Check <a href="https://crankyflier.com/2026/05/xlr/?utm_campaign=x">Cranky Flier XLR post</a>.</p>'
        }
        md = get_entry_show_notes(entry)
        self.assertIn("[Cranky Flier XLR post](https://crankyflier.com/2026/05/xlr/)", md)

    @patch("pipeline.hooks_tal.requests.get")
    def test_tal_episode_page_preserves_body_markdown_links(self, mock_get):
        html_bytes = (
            '<html><body>'
            '<script id="playlist-data">{"archive": "https://www.thisamericanlife.org/sites/default/files/audio/902/a/902.mp3"}</script>'
            '<article class="node-act view-episode">'
            '<div class="field-name-field-act-label">Act One</div>'
            '<h2 class="act-header">Agent Layla</h2>'
            '<div class="field-name-body">Originally from <a href="https://www.cbc.ca/listen/cbc-podcasts/personally?utm_source=tal">Personally: Agent Layla</a>.</div>'
            '</article></body></html>'
        ).encode("utf-8")
        mock_get.return_value = MagicMock(status_code=200, content=html_bytes)
        _, notes, acts = fetch_tal_episode_page("https://www.thisamericanlife.org/902/the-missing-minutes")
        self.assertIn("[Personally: Agent Layla](https://www.cbc.ca/listen/cbc-podcasts/personally)", notes)
        self.assertIn("https://www.cbc.ca/listen/cbc-podcasts/personally", acts[0]["body"])


class TestEntityDisambiguationAndRouting(unittest.TestCase):
    def test_subtype_classification_matrix(self):
        self.assertEqual(
            classify_reference_subtype({"title": "无图之旅：一代青年的自我寻路 (Journey Without a Map)", "creator": "廉思", "category": "book", "note_zh": ""}),
            "book_zh",
        )
        self.assertEqual(
            classify_reference_subtype({"title": "How Asia Works (亚洲大趋势)", "creator": "Joe Studwell", "category": "book", "note_zh": ""}),
            "book_foreign",
        )
        self.assertEqual(
            classify_reference_subtype({"title": "Made in Ethiopia", "creator": "Max Duncan & Xinyan Yu", "category": "media", "note_zh": "2024 年独立观察类纪录片"}),
            "film_doc",
        )
        self.assertEqual(
            classify_reference_subtype({"title": "Cheeky Pint", "creator": "John Collison & Ben Thompson", "category": "media", "note_zh": "在该播客接受采访"}),
            "podcast",
        )
        self.assertEqual(
            classify_reference_subtype({"title": "If I Were King of the Forest", "creator": "Bert Lahr", "category": "media", "note_zh": "电影《绿野仙踪》经典插曲"}),
            "article_or_other",
        )

    def test_wikipedia_disambiguation_blocks_wrong_homonyms(self):
        # 1. Augustus (John Williams novel) must NOT match Roman Emperor Augustus
        augustus_ref = {"title": "Augustus", "creator": "John Williams", "category": "book", "note_zh": "历史书信体小说"}
        self.assertFalse(
            _wiki_candidate_matches(
                augustus_ref,
                "book_foreign",
                "Augustus",
                "Augustus (63 BC – AD 14) was the founder of the Roman Empire and its first emperor.",
                is_disambig=False,
            )
        )
        self.assertTrue(
            _wiki_candidate_matches(
                augustus_ref,
                "book_foreign",
                "Augustus (novel)",
                "Augustus is an epistolary novel by American writer John Williams, published in 1972.",
                is_disambig=False,
            )
        )

        # 2. Home (Marilynne Robinson novel) must NOT match generic Home dwelling or disambiguation page
        home_ref = {"title": "Home", "creator": "Marilynne Robinson", "category": "book", "note_zh": "基列四部曲第二部"}
        self.assertFalse(
            _wiki_candidate_matches(
                home_ref,
                "book_foreign",
                "Home",
                "A home, or domicile, is a space used as a permanent or semi-permanent residence.",
                is_disambig=False,
            )
        )
        self.assertFalse(
            _wiki_candidate_matches(
                home_ref,
                "book_foreign",
                "Home (disambiguation)",
                "Home may refer to: Home (novel) by Marilynne Robinson...",
                is_disambig=True,
            )
        )
        self.assertTrue(
            _wiki_candidate_matches(
                home_ref,
                "book_foreign",
                "Home (Marilynne Robinson novel)",
                "Home is a 2008 novel by Marilynne Robinson.",
                is_disambig=False,
            )
        )

        # 3. Stalingrad (Vasily Grossman novel) must NOT match Battle of Stalingrad
        stalingrad_ref = {"title": "Stalingrad", "creator": "Vasily Grossman", "category": "book", "note_zh": "俄罗斯历史长篇巨作"}
        self.assertFalse(
            _wiki_candidate_matches(
                stalingrad_ref,
                "book_foreign",
                "Battle of Stalingrad",
                "The Battle of Stalingrad (1942–1943) was a major battle on the Eastern Front of World War II.",
                is_disambig=False,
            )
        )
        self.assertTrue(
            _wiki_candidate_matches(
                stalingrad_ref,
                "book_foreign",
                "Stalingrad (novel)",
                "Stalingrad is a 1952 novel by Vasily Grossman about the Battle of Stalingrad.",
                is_disambig=False,
            )
        )

        # 4. Regression guards for real-world false positives caught during 72-item audit
        self.assertFalse(
            _wiki_candidate_matches(
                {"title": "Simple Flying", "creator": "Simple Flying", "category": "media", "note_zh": "航空媒体"},
                "article_or_other",
                "Flying car",
                "A flying car is a type of vehicle that can function both as a road vehicle and as an aircraft.",
                is_disambig=False,
            )
        )
        self.assertFalse(
            _wiki_candidate_matches(
                {"title": "Canada's Indo-Pacific Strategy", "creator": "Government of Canada", "category": "other", "note_zh": ""},
                "article_or_other",
                "Indo-Pacific strategy of South Korea",
                "The Indo-Pacific strategy of South Korea is a regional foreign policy framework.",
                is_disambig=False,
            )
        )
        self.assertFalse(
            _wiki_candidate_matches(
                {"title": "Mr. China (中国通)", "creator": "Tim Clissold", "category": "book", "note_zh": ""},
                "book_foreign",
                "Writers on business in China",
                "Tim Clissold wrote the book Mr. China about business in China.",
                is_disambig=False,
            )
        )
        self.assertFalse(
            _wiki_candidate_matches(
                {"title": "The Ezra Klein Show (Episode with Matt Sheehan)", "creator": "Ezra Klein & Matt Sheehan", "category": "media", "note_zh": "播客单集"},
                "podcast",
                "Ezra Klein",
                "Ezra Klein is an American journalist and podcast host.",
                is_disambig=False,
            )
        )
        self.assertFalse(
            _wiki_candidate_matches(
                {"title": "Miranda July (Substack)", "creator": "Miranda July", "category": "media", "note_zh": "Substack 专栏"},
                "article_or_other",
                "Miranda July",
                "Miranda July is an American film director and writer.",
                is_disambig=False,
            )
        )

    def test_reference_link_normalization_priority_dedup_and_cap(self):
        raw = [
            {"source": "douban", "label": "Wrong Label", "url": "https://book.douban.com/subject/123456/"},
            {"source": "wikipedia", "label": "维基百科", "url": "https://en.wikipedia.org/wiki/How_Asia_Works"},
            {"source": "wikipedia", "label": "Wikipedia", "url": "https://zh.wikipedia.org/wiki/Duplicate"},
            {"source": "goodreads", "label": "GR", "url": "https://www.goodreads.com/book/show/16144575-how-asia-works"},
            {"source": "official", "label": "Official", "url": "https://example.com/official"},
            {"source": "article", "label": "Article", "url": "https://example.com/article"},
        ]
        norm = normalize_reference_links(raw, max_links=4)
        self.assertEqual(len(norm), 4)
        # Priority order: article -> official -> wikipedia -> goodreads (douban capped out as 5th)
        self.assertEqual([x["source"] for x in norm], ["article", "official", "wikipedia", "goodreads"])
        self.assertEqual([x["label"] for x in norm], ["原文", "官网", "Wikipedia", "Goodreads"])

    def test_clamp_and_sort_references_attaches_empty_links_default(self):
        items = [
            ReferenceItem(title="Book A", creator="Author A", category="book", note_zh="说明", start_sec=50)
        ]
        clamped = clamp_and_sort_references(items, duration_sec=100)
        self.assertEqual(len(clamped), 1)
        self.assertEqual(clamped[0]["links"], [])

    def test_show_notes_link_matching_for_article_and_book_guard(self):
        notes_links = [
            {"text": "The Air Current", "url": "https://theaircurrent.com/"},
            {"text": "Home page", "url": "https://example.com/home"},
        ]
        ref_tac = {"title": "The Air Current", "creator": "Jon Ostrower", "category": "media", "note_zh": "航空媒体"}
        matched = match_show_notes_links(ref_tac, notes_links, "article_or_other")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["source"], "official")
        self.assertEqual(matched[0]["url"], "https://theaircurrent.com/")

        # Short 1-word book title 'Home' must NOT match generic /home link
        ref_home = {"title": "Home", "creator": "Marilynne Robinson", "category": "book", "note_zh": "小说"}
        self.assertEqual(match_show_notes_links(ref_home, notes_links, "book_foreign"), [])


class TestEnrichLinksPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_docs_data = Path(tempfile.mkdtemp(prefix="test_enrich_links_"))
        self.show_dir = self.temp_docs_data / "airshow"
        self.ep_dir = self.show_dir / "episodes"
        self.ep_dir.mkdir(parents=True, exist_ok=True)

        self.ep_file = self.ep_dir / "Buzzsprout-200.json"
        self.ep_doc = {
            "schema_version": "3.0",
            "show_id": "airshow",
            "guid": "Buzzsprout-200",
            "title": "Test Link Episode",
            "pub_date": "2026-11-20T00:00:00Z",
            "duration_sec": 1200,
            "audio_url": "https://example.com/200.mp3",
            "summary_zh": "原始摘要绝对不能被修改",
            "key_takeaways_zh": ["原始要点A"],
            "highlights": [{"start_sec": 10, "title_zh": "亮点", "summary_zh": "说明", "quote_en": "Hi"}],
            "vocab_notes": [{"term": "hub", "meaning_zh": "枢纽", "note_zh": "说明", "start_sec": 10}],
            "references": [
                {
                    "title": "Cirium",
                    "creator": "Cirium",
                    "category": "other",
                    "note_zh": "航空数据平台",
                    "start_sec": 30,
                }
            ],
            "transcript": [
                {"id": 1, "start_sec": 0, "speaker": "Jon Ostrower", "en": "Hello.", "zh": "你好。"}
            ],
        }
        self.ep_file.write_text(json.dumps(self.ep_doc, ensure_ascii=False, indent=2), encoding="utf-8")

        self.index_file = self.show_dir / "index.json"
        self.index_raw = json.dumps(
            {
                "schema_version": "3.0",
                "show_id": "airshow",
                "podcast_title": "The Air Show",
                "rss_url": "https://example.com/rss",
                "updated_at": "2026-11-20T00:00:00Z",
                "episodes": [
                    {
                        "guid": "Buzzsprout-200",
                        "title": "Test Link Episode",
                        "pub_date": "2026-11-20T00:00:00Z",
                        "duration_sec": 1200,
                        "audio_url": "https://example.com/200.mp3",
                        "file": "episodes/Buzzsprout-200.json",
                        "summary_zh": "原始摘要绝对不能被修改",
                        "key_takeaways_zh": ["原始要点A"],
                        "highlights_count": 1,
                        "transcript_count": 1,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        self.index_file.write_text(self.index_raw, encoding="utf-8")

        self.config = {
            "shows": [
                {
                    "id": "airshow",
                    "title": "The Air Show",
                    "subtitle": "Aviation",
                    "rss_url": "https://example.com/rss",
                    "enabled": True,
                }
            ]
        }

    def tearDown(self):
        shutil.rmtree(self.temp_docs_data, ignore_errors=True)

    @patch("pipeline.runner.resolve_references_links")
    @patch("pipeline.runner.fetch_rss_show_notes_map", return_value={})
    def test_enrich_links_idempotent_preserves_other_fields_and_supports_force(self, mock_rss, mock_resolve):
        mock_resolve.return_value = [
            {
                **self.ep_doc["references"][0],
                "links": [{"source": "official", "label": "官网", "url": "https://www.cirium.com/"}],
            }
        ]

        # 1. Dry-run touches nothing and makes no resolver calls
        dry_args = argparse.Namespace(show="airshow", limit=0, dry_run=True, force_links=False)
        with patch("pipeline.runner.DOCS_DATA_DIR", self.temp_docs_data):
            run_enrich_links_pipeline(dry_args, self.config)
        mock_resolve.assert_not_called()
        self.assertNotIn("links", json.loads(self.ep_file.read_text("utf-8"))["references"][0])

        # 2. First real run backfills links, preserves all other fields & index.json byte-for-byte
        run_args = argparse.Namespace(show="airshow", limit=0, dry_run=False, force_links=False)
        with patch("pipeline.runner.DOCS_DATA_DIR", self.temp_docs_data):
            run_enrich_links_pipeline(run_args, self.config)
        self.assertEqual(mock_resolve.call_count, 1)
        self.assertEqual(self.index_file.read_text("utf-8"), self.index_raw)
        updated = json.loads(self.ep_file.read_text("utf-8"))
        self.assertEqual(updated["summary_zh"], self.ep_doc["summary_zh"])
        self.assertEqual(updated["transcript"], self.ep_doc["transcript"])
        self.assertEqual(updated["vocab_notes"], self.ep_doc["vocab_notes"])
        self.assertEqual(
            updated["references"][0]["links"],
            [{"source": "official", "label": "官网", "url": "https://www.cirium.com/"}],
        )

        # 3. Second run without --force-links skips cleanly (idempotent)
        with patch("pipeline.runner.DOCS_DATA_DIR", self.temp_docs_data):
            run_enrich_links_pipeline(run_args, self.config)
        self.assertEqual(mock_resolve.call_count, 1)

        # 4. Run with --force-links re-resolves
        force_args = argparse.Namespace(show="airshow", limit=0, dry_run=False, force_links=True)
        with patch("pipeline.runner.DOCS_DATA_DIR", self.temp_docs_data):
            run_enrich_links_pipeline(force_args, self.config)
        self.assertEqual(mock_resolve.call_count, 2)


class TestValidatorReferenceLinksGate(unittest.TestCase):
    def test_valid_links_pass_cleanly(self):
        rep = validate_data.ValidationReporter(strict=True)
        ref = {
            "title": "How Asia Works",
            "links": [
                {"source": "wikipedia", "label": "Wikipedia", "url": "https://en.wikipedia.org/wiki/How_Asia_Works"},
                {"source": "goodreads", "label": "Goodreads", "url": "https://www.goodreads.com/book/show/16144575-how-asia-works"},
                {"source": "douban", "label": "豆瓣", "url": "https://book.douban.com/subject/26365483/"},
            ],
        }
        validate_data.validate_reference_links("[sinica][ep1]", 0, ref, rep)
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.warnings, [])

    def test_missing_links_warns_and_fails_strict(self):
        rep = validate_data.ValidationReporter(strict=True)
        validate_data.validate_reference_links("[sinica][ep1]", 0, {"title": "Legacy Ref"}, rep)
        self.assertEqual(rep.errors, [])
        self.assertEqual(len(rep.warnings), 1)
        self.assertTrue(rep.has_failures)

    def test_bad_links_trigger_hard_errors(self):
        cases = [
            # Search page URL
            [{"source": "douban", "label": "豆瓣", "url": "https://search.douban.com/book/subject_search?search_text=foo"}],
            # Shortlink domain
            [{"source": "official", "label": "官网", "url": "https://bit.ly/abc123"}],
            # Wrong label mapping
            [{"source": "douban", "label": "Douban", "url": "https://book.douban.com/subject/12345/"}],
            # Duplicate source
            [
                {"source": "wikipedia", "label": "Wikipedia", "url": "https://en.wikipedia.org/wiki/A"},
                {"source": "wikipedia", "label": "Wikipedia", "url": "https://zh.wikipedia.org/wiki/A"},
            ],
            # Exceeds 4 links
            [
                {"source": "article", "label": "原文", "url": "https://a.example/1"},
                {"source": "official", "label": "官网", "url": "https://a.example/2"},
                {"source": "wikipedia", "label": "Wikipedia", "url": "https://en.wikipedia.org/wiki/A"},
                {"source": "goodreads", "label": "Goodreads", "url": "https://www.goodreads.com/book/show/1"},
                {"source": "douban", "label": "豆瓣", "url": "https://book.douban.com/subject/1/"},
            ],
            # Bare RSS feed as podcast_site
            [{"source": "podcast_site", "label": "播客官网", "url": "https://feeds.buzzsprout.com/2329718.rss"}],
            # Non-http protocol
            [{"source": "official", "label": "官网", "url": "javascript:alert(1)"}],
        ]
        for bad_links in cases:
            rep = validate_data.ValidationReporter(strict=False)
            validate_data.validate_reference_links("[test][ep]", 0, {"title": "T", "links": bad_links}, rep)
            self.assertGreater(len(rep.errors), 0, f"Expected hard error for links: {bad_links}")


if __name__ == "__main__":
    unittest.main()
