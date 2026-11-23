"""Unit tests for pipeline stability hardening:
1. Channel B breakpoint cache (write, hit skip, atomic delete, failure retention, smoke isolation, --no-cache)
2. Channel A transcript serialization (only [MM:SS] <speaker>: <en>, no zh, input_chars logging)
3. Transport disconnection disaster recovery and consecutive disconnect thinking step-down
"""

import argparse
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pydantic import BaseModel

from pipeline.config import (
    get_next_fallback_thinking_level,
    resolve_ch_a_thinking_level,
)
from pipeline.gemini import (
    create_genai_client,
    generate_structured_with_retry,
    is_transport_disconnect_or_timeout,
    run_channel_a,
)
from pipeline.models import ChannelAOutput
from pipeline.runner import process_episode
from pipeline.storage import (
    CH_B_CACHE_DIR,
    delete_ch_b_cache,
    get_ch_b_cache_path,
    load_ch_b_cache,
    save_ch_b_cache,
)


class DummyOutput(BaseModel):
    summary: str


class TestChannelBCache(unittest.TestCase):
    """Test Channel B intermediate breakpoint cache behavior."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_cache_"))
        self.orig_cache_dir = CH_B_CACHE_DIR
        # Patch storage.CH_B_CACHE_DIR
        self.patcher = patch("pipeline.storage.CH_B_CACHE_DIR", self.test_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_load_cache(self):
        show_id = "tal"
        guid = "https://www.thisamericanlife.org/850/test"
        transcript = [
            {"id": 1, "start_sec": 0, "speaker": "Ira Glass", "en": "Hello.", "zh": "你好。"}
        ]
        save_ch_b_cache(
            show_id=show_id,
            guid=guid,
            chunk_sec=180,
            duration_sec=3600,
            audio_url="https://audio.example.com/test.mp3",
            transcript=transcript,
        )

        cache_path = get_ch_b_cache_path(show_id, guid)
        self.assertTrue(cache_path.exists())

        loaded = load_ch_b_cache(show_id, guid, chunk_sec=180)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["show_id"], show_id)
        self.assertEqual(loaded["guid"], guid)
        self.assertEqual(loaded["chunk_sec"], 180)
        self.assertEqual(loaded["duration_sec"], 3600)
        self.assertEqual(loaded["transcript"], transcript)

    def test_load_cache_mismatch_returns_none(self):
        show_id = "tal"
        guid = "https://www.thisamericanlife.org/850/test"
        save_ch_b_cache(
            show_id=show_id,
            guid=guid,
            chunk_sec=180,
            duration_sec=3600,
            audio_url="https://audio.example.com/test.mp3",
            transcript=[{"id": 1, "start_sec": 0, "speaker": "Ira Glass", "en": "Hi", "zh": "嗨"}],
        )

        # Different chunk_sec -> should return None
        self.assertIsNone(load_ch_b_cache(show_id, guid, chunk_sec=120))
        # Different show_id -> should return None
        self.assertIsNone(load_ch_b_cache("airshow", guid, chunk_sec=180))

    def test_delete_cache(self):
        show_id = "tal"
        guid = "test_guid_123"
        save_ch_b_cache(show_id, guid, 180, 100, "http://a.mp3", [{"id": 1}])
        self.assertTrue(get_ch_b_cache_path(show_id, guid).exists())

        deleted = delete_ch_b_cache(show_id, guid)
        self.assertTrue(deleted)
        self.assertFalse(get_ch_b_cache_path(show_id, guid).exists())


class TestPipelineCacheIntegration(unittest.TestCase):
    """Test process_episode cache hit/skip, save/delete, and isolation."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_pipeline_cache_"))
        self.patcher = patch("pipeline.storage.CH_B_CACHE_DIR", self.test_dir)
        self.patcher.start()

        self.entry = {
            "id": "ep1",
            "guid": "ep1",
            "title": "Episode 1",
            "published": "Mon, 20 Nov 2026 12:00:00 GMT",
            "enclosures": [{"href": "https://example.com/ep1.mp3"}],
        }
        self.show = {
            "id": "tal",
            "title": "This American Life",
            "subtitle": "Test Show",
            "content_style": "narrative",
        }

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch("pipeline.runner.atomic_save_episode")
    @patch("pipeline.runner.run_channel_a")
    @patch("pipeline.runner.run_channel_b")
    @patch("pipeline.runner.chunk_audio")
    @patch("pipeline.runner.download_audio_file")
    def test_cache_hit_skips_download_chunk_and_ch_b(
        self, mock_download, mock_chunk, mock_ch_b, mock_ch_a, mock_save
    ):
        # Pre-seed cache
        guid = "ep1"
        sample_transcript = [{"id": 1, "start_sec": 0, "speaker": "Ira", "en": "Test", "zh": "测试"}]
        save_ch_b_cache(
            show_id="tal",
            guid=guid,
            chunk_sec=180,
            duration_sec=1200,
            audio_url="https://example.com/ep1.mp3",
            transcript=sample_transcript,
        )

        args = argparse.Namespace(
            chunk_sec=180,
            max_chunks=0,
            dry_run=False,
            no_cache=False,
        )
        mock_ch_a.return_value = {
            "summary_zh": "测试摘要",
            "key_takeaways_zh": ["要点1"],
            "highlights": [],
            "vocab_notes": [],
            "references": [],
        }

        process_episode(self.entry, args, self.show, glossary_text="")

        # Verify download, chunk, and ch_b were NOT called
        mock_download.assert_not_called()
        mock_chunk.assert_not_called()
        mock_ch_b.assert_not_called()

        # Verify Channel A was called with cached transcript
        mock_ch_a.assert_called_once()
        self.assertEqual(mock_ch_a.call_args[1]["transcript"], sample_transcript)

        # Verify atomic_save was called and cache was deleted after successful save
        mock_save.assert_called_once()
        self.assertFalse(get_ch_b_cache_path("tal", guid).exists())

    @patch("pipeline.runner.atomic_save_episode")
    @patch("pipeline.runner.run_channel_a")
    @patch("pipeline.runner.run_channel_b")
    @patch("pipeline.runner.chunk_audio")
    @patch("pipeline.runner.download_audio_file")
    @patch("pipeline.runner.get_audio_duration_sec")
    def test_cache_retained_on_ch_a_failure(
        self, mock_duration, mock_download, mock_chunk, mock_ch_b, mock_ch_a, mock_save
    ):
        guid = "ep1"
        mock_duration.return_value = 1200
        mock_file = MagicMock(spec=Path)
        mock_file.name = "chunk_0.mp3"
        mock_file.stat.return_value.st_size = 1024
        mock_chunk.return_value = [mock_file]
        sample_transcript = [{"id": 1, "start_sec": 0, "speaker": "Ira", "en": "Test", "zh": "测试"}]
        mock_ch_b.return_value = sample_transcript

        # Simulate CH-A crash
        mock_ch_a.side_effect = RuntimeError("Server disconnected without sending a response")

        args = argparse.Namespace(
            chunk_sec=180,
            max_chunks=0,
            dry_run=False,
            no_cache=False,
        )

        with self.assertRaises(RuntimeError):
            process_episode(self.entry, args, self.show, glossary_text="")

        # Cache file must still exist!
        cache_path = get_ch_b_cache_path("tal", guid)
        self.assertTrue(cache_path.exists())
        loaded = load_ch_b_cache("tal", guid, 180)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["transcript"], sample_transcript)

    @patch("pipeline.runner.save_smoke_preview")
    @patch("pipeline.runner.run_channel_a")
    @patch("pipeline.runner.run_channel_b")
    @patch("pipeline.runner.chunk_audio")
    @patch("pipeline.runner.download_audio_file")
    @patch("pipeline.runner.get_audio_duration_sec")
    def test_smoke_mode_isolated_from_cache(
        self, mock_duration, mock_download, mock_chunk, mock_ch_b, mock_ch_a, mock_smoke
    ):
        guid = "ep1"
        mock_duration.return_value = 1200
        mock_file = MagicMock(spec=Path)
        mock_file.name = "chunk_0.mp3"
        mock_file.stat.return_value.st_size = 1024
        mock_chunk.return_value = [mock_file]
        mock_ch_b.return_value = [{"id": 1, "start_sec": 0, "speaker": "Ira", "en": "Hi", "zh": "嗨"}]
        mock_ch_a.return_value = {"summary_zh": "test", "key_takeaways_zh": []}

        # max_chunks = 2 (smoke mode)
        args = argparse.Namespace(
            chunk_sec=180,
            max_chunks=2,
            dry_run=False,
            no_cache=False,
        )

        process_episode(self.entry, args, self.show, glossary_text="")

        # Cache must NOT be written in smoke mode
        self.assertFalse(get_ch_b_cache_path("tal", guid).exists())

    @patch("pipeline.runner.atomic_save_episode")
    @patch("pipeline.runner.run_channel_a")
    @patch("pipeline.runner.run_channel_b")
    @patch("pipeline.runner.chunk_audio")
    @patch("pipeline.runner.download_audio_file")
    @patch("pipeline.runner.get_audio_duration_sec")
    def test_no_cache_ignores_existing_cache(
        self, mock_duration, mock_download, mock_chunk, mock_ch_b, mock_ch_a, mock_save
    ):
        guid = "ep1"
        # Seed cache
        save_ch_b_cache("tal", guid, 180, 1000, "http://a.mp3", [{"id": 1, "en": "Old"}])

        mock_duration.return_value = 1200
        mock_file = MagicMock(spec=Path)
        mock_file.name = "chunk_0.mp3"
        mock_file.stat.return_value.st_size = 1024
        mock_chunk.return_value = [mock_file]
        fresh_transcript = [{"id": 1, "start_sec": 0, "speaker": "Ira", "en": "Fresh", "zh": "新鲜"}]
        mock_ch_b.return_value = fresh_transcript
        mock_ch_a.return_value = {"summary_zh": "test"}

        # args with no_cache=True
        args = argparse.Namespace(
            chunk_sec=180,
            max_chunks=0,
            dry_run=False,
            no_cache=True,
        )

        process_episode(self.entry, args, self.show, glossary_text="")

        # download and ch_b MUST be called because cache was ignored
        mock_download.assert_called_once()
        mock_ch_b.assert_called_once()


class TestChannelATranscriptSlimming(unittest.TestCase):
    """Test Channel A input transcript serialization strictly contains only [MM:SS] <speaker>: <en>."""

    @patch("pipeline.gemini.generate_structured_with_retry")
    def test_channel_a_serialization_no_zh_and_format(self, mock_gen):
        mock_output = ChannelAOutput(
            summary_zh="测试中文总结",
            key_takeaways_zh=["分幕导读1", "分幕导读2", "分幕导读3"],
            highlights=[],
            vocab_notes=[],
            references=[],
        )
        mock_gen.return_value = mock_output

        transcript = [
            {"id": 1, "start_sec": 0, "speaker": "Ira Glass", "en": "Welcome to This American Life.", "zh": "欢迎收听《美国生活》。"},
            {"id": 2, "start_sec": 65, "speaker": "Guest", "en": "Thank you for having me.", "zh": "谢谢邀请我。"},
            {"id": 3, "start_sec": 3661, "speaker": "Ira Glass", "en": "That's all for today.", "zh": "今天就到这里。"},
        ]

        with patch("pipeline.gemini.create_genai_client") as mock_client_factory:
            mock_client = MagicMock()
            mock_client_factory.return_value = mock_client
            res = run_channel_a(
                transcript=transcript,
                glossary_text="Glossary",
                title="Test Ep",
                duration_sec=3700,
                show_notes="Notes",
                show_title="This American Life",
                show_subtitle="Narrative",
                show_id="tal",
                content_style="narrative",
            )

        self.assertIn("summary_zh", res)
        # Check contents passed to generate_structured_with_retry
        call_args = mock_gen.call_args[1]
        user_prompt = call_args["contents"][0]

        # Must contain format [MM:SS] <speaker>: <en>
        self.assertIn("[00:00] Ira Glass: Welcome to This American Life.", user_prompt)
        self.assertIn("[01:05] Guest: Thank you for having me.", user_prompt)
        self.assertIn("[01:01:01] Ira Glass: That's all for today.", user_prompt)

        # Strictly MUST NOT contain Chinese translations
        self.assertNotIn("欢迎收听《美国生活》", user_prompt)
        self.assertNotIn("谢谢邀请我", user_prompt)
        self.assertNotIn("今天就到这里", user_prompt)

        # Strictly MUST NOT contain redundant seconds like '(0s)' or '(65s)'
        self.assertNotIn("(0s)", user_prompt)
        self.assertNotIn("(65s)", user_prompt)


class TestTransportDisasterRecovery(unittest.TestCase):
    """Test transport disconnect detection, client rebuild, and thinking downgrade chain."""

    def test_transport_disconnect_identification(self):
        # Known disconnect strings
        self.assertTrue(is_transport_disconnect_or_timeout(Exception("Server disconnected without sending a response")))
        self.assertTrue(is_transport_disconnect_or_timeout(Exception("Connection reset by peer")))
        self.assertTrue(is_transport_disconnect_or_timeout(Exception("The read operation timed out")))
        self.assertTrue(is_transport_disconnect_or_timeout(Exception("RemoteProtocolError: Server disconnected")))

        # Non-transport errors
        self.assertFalse(is_transport_disconnect_or_timeout(ValueError("Invalid json format")))
        self.assertFalse(is_transport_disconnect_or_timeout(RuntimeError("Model 404 NOT_FOUND")))

    def test_thinking_fallback_chain(self):
        self.assertEqual(get_next_fallback_thinking_level("xhigh"), "high")
        self.assertEqual(get_next_fallback_thinking_level("high"), "low")
        self.assertEqual(get_next_fallback_thinking_level("low"), None)
        self.assertEqual(get_next_fallback_thinking_level(None), None)

    def test_http_options_timeout_value(self):
        # create_genai_client must set timeout to 300,000 ms (300 seconds)
        with patch.dict("os.environ", {"GEMINI_API_KEY": "fake_key"}):
            client = create_genai_client()
            timeout_ms = client._api_client._http_options.timeout
            self.assertEqual(timeout_ms, 300000)

    @patch("time.sleep")
    def test_consecutive_disconnect_downgrades_thinking_level_and_rebuilds_client(self, mock_sleep):
        """Test:
        Initial thinking_level='xhigh'
        Attempt 1: disconnect -> rebuild client, thinking stays xhigh (consecutive=1)
        Attempt 2: disconnect -> rebuild client, consecutive=2 -> step down to 'high'
        Attempt 3: disconnect -> rebuild client, thinking stays high (consecutive=1)
        Attempt 4: disconnect -> rebuild client, consecutive=2 -> step down to 'low'
        Attempt 5: success at effective thinking_level='low'!
        """
        captured_thinking_levels = []
        clients_created = []

        def mock_client_factory():
            c = MagicMock()
            clients_created.append(c)
            return c

        class ChunkMock:
            def __init__(self, text):
                self.text = text

        first_client = mock_client_factory()

        def fake_generate_stream(*args, **kwargs):
            cfg = kwargs.get("config")
            th_cfg = getattr(cfg, "thinking_config", None)
            lvl = getattr(th_cfg, "thinking_level", None) if th_cfg else None
            captured_thinking_levels.append(lvl)

            if len(captured_thinking_levels) in (1, 2, 3, 4):
                raise Exception("Server disconnected without sending a response")
            return [ChunkMock('{"summary": "success"}')]

        # Whenever a client is created, its models.generate_content_stream uses fake_generate_stream
        for c in clients_created:
            c.models.generate_content_stream.side_effect = fake_generate_stream

        def dynamic_client_factory():
            c = MagicMock()
            c.models.generate_content_stream.side_effect = fake_generate_stream
            clients_created.append(c)
            return c

        first_client.models.generate_content_stream.side_effect = fake_generate_stream

        result = generate_structured_with_retry(
            client=first_client,
            model_pool=["test-model"],
            contents=["test prompt"],
            system_instruction="system",
            response_schema=DummyOutput,
            stage="TEST",
            max_retries=5,
            thinking_level="xhigh",
            client_factory=dynamic_client_factory,
        )

        self.assertEqual(result.summary, "success")
        # Check captured thinking levels:
        # Attempt 1: xhigh
        # Attempt 2: xhigh (2 consecutive disconnects -> steps down to high)
        # Attempt 3: high
        # Attempt 4: high (2 consecutive disconnects -> steps down to low)
        # Attempt 5: low (succeeded!)
        normalized_levels = [
            (lvl.value if hasattr(lvl, "value") else str(lvl)).lower()
            if lvl is not None else None
            for lvl in captured_thinking_levels
        ]
        self.assertEqual(normalized_levels, ["xhigh", "xhigh", "high", "high", "low"])
        # Check that client.close was called on disconnected clients
        for c in clients_created[:-1]:
            c.close.assert_called()

    @patch("time.sleep")
    def test_thinking_level_downgrades_from_low_to_none(self, mock_sleep):
        """Test thinking level starting at 'low' downgrades to None on 2 consecutive disconnects."""
        captured_thinking_levels = []
        clients_created = []

        class ChunkMock:
            def __init__(self, text):
                self.text = text

        def fake_generate_stream(*args, **kwargs):
            cfg = kwargs.get("config")
            th_cfg = getattr(cfg, "thinking_config", None)
            lvl = getattr(th_cfg, "thinking_level", None) if th_cfg else None
            captured_thinking_levels.append(lvl)

            if len(captured_thinking_levels) in (1, 2):
                raise Exception("Server disconnected without sending a response")
            return [ChunkMock('{"summary": "recovered_none"}')]

        def dynamic_client_factory():
            c = MagicMock()
            c.models.generate_content_stream.side_effect = fake_generate_stream
            clients_created.append(c)
            return c

        first_client = dynamic_client_factory()

        result = generate_structured_with_retry(
            client=first_client,
            model_pool=["test-model"],
            contents=["test prompt"],
            system_instruction="system",
            response_schema=DummyOutput,
            stage="TEST",
            max_retries=3,
            thinking_level="low",
            client_factory=dynamic_client_factory,
        )

        self.assertEqual(result.summary, "recovered_none")
        normalized_levels = [
            (lvl.value if hasattr(lvl, "value") else str(lvl)).lower()
            if lvl is not None else None
            for lvl in captured_thinking_levels
        ]
        self.assertEqual(normalized_levels, ["low", "low", None])


if __name__ == "__main__":
    unittest.main()
