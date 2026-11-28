"""Unit tests for scripts/agent_prep.py zero-API Antigravity Agent bridge CLI."""

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

import agent_prep


class TestAgentPrepBridge(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_agent_prep_"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_classify_episode_slot(self):
        existing = ["2026-09-01T10:00:00Z", "2026-11-01T10:00:00Z"]
        self.assertEqual(agent_prep.classify_episode_slot("2026-11-15T00:00:00Z", existing), "latest")
        self.assertEqual(agent_prep.classify_episode_slot("2026-10-10T00:00:00Z", existing), "gap")
        self.assertEqual(agent_prep.classify_episode_slot("2026-08-15T00:00:00Z", existing), "archive")
        self.assertEqual(agent_prep.classify_episode_slot("2026-08-15T00:00:00Z", []), "latest")

    def test_continuity_prompt_and_merge_ch_b_chunks(self):
        work_dir = self.tmp_dir / "airshow__ep-test-1"
        ch_b_dir = work_dir / "ch_b_chunks"
        ch_b_dir.mkdir(parents=True)

        c0_json = ch_b_dir / "chunk_000.json"
        c1_json = ch_b_dir / "chunk_001.json"

        manifest = {
            "schema_version": "1.0",
            "show_id": "airshow",
            "show_title": "The Air Show",
            "show_subtitle": "Aviation Business",
            "content_style": "analytical",
            "guid": "ep-test-1",
            "safe_guid": "ep-test-1",
            "title": "Test Episode",
            "pub_date": "2026-11-26T10:00:00Z",
            "duration_sec": 550,
            "audio_url": "https://example.com/ep1.mp3",
            "chunk_sec": 300,
            "show_notes": "Notes",
            "speaker_roster": [],
            "ch_a_output_path": str(work_dir / "ch_a.json"),
            "ch_a_user_prompt_path": str(work_dir / "ch_a_user_prompt.txt"),
            "chunks": [
                {
                    "index": 0,
                    "name": "chunk_000.mp3",
                    "path": str(work_dir / "chunks" / "chunk_000.mp3"),
                    "output_json_path": str(c0_json),
                    "duration_sec": 300,
                    "start_offset_sec": 0,
                },
                {
                    "index": 1,
                    "name": "chunk_001.mp3",
                    "path": str(work_dir / "chunks" / "chunk_001.mp3"),
                    "output_json_path": str(c1_json),
                    "duration_sec": 250,
                    "start_offset_sec": 300,
                },
            ],
        }
        (work_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Write chunk 0 output with title prefix & slightly out-of-order second timestamp
        c0_json.write_text(
            json.dumps({
                "segments": [
                    {"timestamp": "00:05", "speaker": "Dr. Brian Sumers", "en": "Welcome back.", "zh": "欢迎回来。"},
                    {"timestamp": "04:58", "speaker": "Brett Snyder", "en": "Let us look at capacity.", "zh": "我们来看看运力。"},
                ]
            }),
            encoding="utf-8",
        )

        # Continuity prompt for chunk 1 should carry normalized speaker names & tail segment
        p1 = agent_prep.build_next_ch_b_user_prompt(work_dir, 1)
        self.assertIn("Brian Sumers, Brett Snyder", p1)
        self.assertIn("Let us look at capacity.", p1)

        # Write chunk 1 output with timestamp exceeding chunk_dur (260s > 250s) to test clamping
        c1_json.write_text(
            json.dumps({
                "segments": [
                    {"timestamp": "00:02", "speaker": "Brett Snyder", "en": "Specifically transatlantic.", "zh": "具体来说是跨大西洋航线。"},
                    {"timestamp": "04:30", "speaker": "Brian Sumers", "en": "That wraps it up.", "zh": "以上就是本期内容。"},
                ]
            }),
            encoding="utf-8",
        )

        fake_cache_dir = self.tmp_dir / "ch_b_cache"
        with patch("pipeline.storage.CH_B_CACHE_DIR", fake_cache_dir):
            merged = agent_prep.merge_ch_b_chunks(work_dir)
            self.assertEqual(len(merged), 4)
            self.assertEqual([m["id"] for m in merged], [1, 2, 3, 4])
            self.assertEqual(merged[0]["speaker"], "Brian Sumers")
            self.assertEqual(merged[0]["start_sec"], 5)
            self.assertEqual(merged[1]["start_sec"], 298)
            self.assertEqual(merged[2]["start_sec"], 302)
            # 04:30 = 270s, clamped to chunk_dur=250 -> 300 + 250 = 550
            self.assertEqual(merged[3]["start_sec"], 550)
            self.assertTrue((work_dir / "ch_a_user_prompt.txt").exists())
            self.assertTrue((fake_cache_dir / "airshow__ep-test-1.json").exists())

    @patch("agent_prep.requests.get")
    def test_scrape_tal_archive_entry_by_number(self, mock_get):
        html = """
        <html>
          <head>
            <link rel="shortlink" href="https://www.thisamericanlife.org/node/46200" />
            <meta property="article:published_time" content="2026-10-25T01:00:00Z" />
          </head>
          <body class="page-node-46200">
            <h1 class="node-title">899: Some Classic Story</h1>
          </body>
        </html>
        """
        resp = MagicMock()
        resp.status_code = 200
        resp.url = "https://www.thisamericanlife.org/899/some-classic-story"
        resp.content = html.encode("utf-8")
        mock_get.return_value = resp

        entry = agent_prep.scrape_tal_archive_entry_by_number(899)
        self.assertEqual(entry["guid"], "46200 at https://www.thisamericanlife.org")
        self.assertEqual(entry["title"], "899: Some Classic Story")
        self.assertEqual(entry["pub_date"] if "pub_date" in entry else entry["published"], "2026-10-25T01:00:00Z")

    @patch("agent_prep.requests.get")
    def test_scrape_tal_archive_404_resolves_slug_from_transcript(self, mock_get):
        r404 = MagicMock(status_code=404, url="https://www.thisamericanlife.org/585", content=b"Not Found")
        r_tx = MagicMock(
            status_code=200,
            url="https://www.thisamericanlife.org/585/transcript",
            content=b'<html><body><a href="/585/in-defense-of-ignorance">In Defense of Ignorance</a></body></html>',
        )
        r_ep = MagicMock(
            status_code=200,
            url="https://www.thisamericanlife.org/585/in-defense-of-ignorance",
            content=(
                b'<html><head><link rel="shortlink" href="https://www.thisamericanlife.org/node/38585" />'
                b'<meta property="article:published_time" content="2016-04-22T01:00:00Z" /></head>'
                b'<body><h1>In Defense of Ignorance</h1></body></html>'
            ),
        )
        mock_get.side_effect = [r404, r_tx, r_ep]
        entry = agent_prep.scrape_tal_archive_entry_by_number(585)
        self.assertEqual(entry["guid"], "38585 at https://www.thisamericanlife.org")
        self.assertEqual(entry["title"], "585: In Defense of Ignorance")
        self.assertEqual(entry["link"], "https://www.thisamericanlife.org/585/in-defense-of-ignorance")
        self.assertEqual(entry["published"], "2016-04-22T01:00:00Z")


if __name__ == "__main__":
    unittest.main()
