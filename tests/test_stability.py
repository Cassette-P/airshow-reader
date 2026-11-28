"""Unit tests for pipeline stability hardening:
1. Channel B breakpoint cache (write, hit skip, atomic delete, failure retention, smoke isolation, --no-cache)
2. Channel A transcript serialization (only [MM:SS] <speaker>: <en>, no zh, input_chars logging)
3. Transport disconnection disaster recovery and consecutive disconnect thinking step-down
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

from pydantic import BaseModel

from pipeline.config import (
    DEFAULT_CH_A_POOL,
    get_ch_b_api_keys,
    get_ch_b_chunk_sec,
    get_ch_b_free_rpd,
    get_ch_b_free_rpm,
    get_ch_b_overflow_to_paid,
    get_next_fallback_thinking_level,
    resolve_ch_a_thinking_level,
)
from pipeline.gemini import (
    CH_B_QUOTA_STATE,
    QuotaExhaustedRPDError,
    SlidingWindowRateLimiter,
    create_genai_client,
    extract_interaction_text,
    generate_channel_a_structured_with_retry,
    generate_structured_with_retry,
    is_api_key_auth_error,
    is_rate_limit_error,
    is_rpd_exhausted_error,
    is_service_unavailable_error,
    is_transport_disconnect_or_timeout,
    parse_retry_delay_sec,
    reset_ch_b_quota_state,
    run_channel_a,
    run_channel_b,
)
from pipeline.hooks_tal import (
    extract_tal_episode_number,
    fetch_tal_episode_page,
    fetch_tal_transcript_page,
    filter_tal_episodes,
)
from pipeline.models import ChannelAOutput, EnrichmentResponse, ReferenceItem, VocabNote
from pipeline.runner import process_episode, run_enrichment_pipeline, run_main_pipeline
from pipeline.storage import (
    CH_B_CACHE_DIR,
    delete_ch_b_cache,
    get_ch_b_cache_path,
    load_ch_b_cache,
    save_ch_b_cache,
    sync_shows_manifest,
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

    @patch("pipeline.gemini.generate_channel_a_structured_with_retry")
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


class TestChannelADualTransport(unittest.TestCase):
    """Test Channel A dual-transport architecture:
    1. interactions_bg submit -> multiple in_progress polling -> completed parsed JSON
    2. Polling phase handles transient network errors (RemoteProtocolError) by rebuilding client without re-creating interaction
    3. Missing interactions attribute or unsupported error seamlessly falls back to stream_with_thoughts with include_thoughts=True, skipping thought parts
    4. Terminal incomplete or timeout in interactions enters outer candidate retry loop
    """

    @patch("time.sleep")
    def test_interactions_bg_submit_polling_completed(self, mock_sleep):
        """Test happy path: interactions.create(background=True) -> 2 in_progress polls -> completed."""
        mock_client = MagicMock()
        mock_interaction = MagicMock(id="test_interaction_001")
        mock_client.interactions.create.return_value = mock_interaction

        poll_resps = [
            MagicMock(status="in_progress"),
            MagicMock(status="in_progress"),
            MagicMock(
                status="completed",
                output_text='{"summary_zh": "成功摘要", "key_takeaways_zh": ["要点1"], "highlights": []}',
                usage=MagicMock(total_input_tokens=120, total_thought_tokens=240, total_output_tokens=60),
            ),
        ]
        mock_client.interactions.get.side_effect = poll_resps

        result = generate_channel_a_structured_with_retry(
            client=mock_client,
            model_pool=["gemini-4-carbon"],
            user_prompt="Summarize this podcast",
            system_instruction="System prompt",
            response_schema=ChannelAOutput,
            temperature=0.2,
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=lambda: mock_client,
        )

        self.assertEqual(result.summary_zh, "成功摘要")
        self.assertEqual(result.key_takeaways_zh, ["要点1"])

        # Verify create parameters
        mock_client.interactions.create.assert_called_once()
        create_kwargs = mock_client.interactions.create.call_args[1]
        self.assertEqual(create_kwargs["model"], "gemini-4-carbon")
        self.assertTrue(create_kwargs["background"])
        self.assertTrue(create_kwargs.get("store", True))
        self.assertNotIn("response_mime_type", create_kwargs)
        self.assertEqual(create_kwargs["generation_config"]["max_output_tokens"], 65536)
        self.assertEqual(create_kwargs["generation_config"]["thinking_level"], "high")
        self.assertEqual(create_kwargs["response_format"]["type"], "text")
        self.assertEqual(create_kwargs["response_format"]["mime_type"], "application/json")
        self.assertIn("schema", create_kwargs["response_format"])

        # Verify polling calls
        self.assertEqual(mock_client.interactions.get.call_count, 3)
        mock_client.interactions.get.assert_called_with(id="test_interaction_001")

    @patch("time.sleep")
    def test_interactions_bg_polling_network_jitter_rebuilds_client_without_recreate(self, mock_sleep):
        """Test network disconnect during polling rebuilds client and resumes polling the same interaction id without re-creating."""
        client1 = MagicMock()
        client2 = MagicMock()

        mock_interaction = MagicMock(id="test_interaction_jitter")
        client1.interactions.create.return_value = mock_interaction
        # First poll on client1 raises RemoteProtocolError
        client1.interactions.get.side_effect = Exception("RemoteProtocolError: Server disconnected")

        # Second poll on client2 succeeds
        client2.interactions.get.return_value = MagicMock(
            status="completed",
            output_text='{"summary_zh": "抖动恢复成功", "key_takeaways_zh": ["恢复要点"], "highlights": []}',
            usage=MagicMock(total_input_tokens=150, total_thought_tokens=300, total_output_tokens=70),
        )

        client_list = [client1, client2]

        def test_client_factory():
            if client_list:
                return client_list.pop(0)
            return MagicMock()

        initial_client = test_client_factory()

        result = generate_channel_a_structured_with_retry(
            client=initial_client,
            model_pool=["gemini-4-carbon"],
            user_prompt="Summarize this podcast",
            system_instruction="System prompt",
            response_schema=ChannelAOutput,
            temperature=0.2,
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=test_client_factory,
        )

        self.assertEqual(result.summary_zh, "抖动恢复成功")
        # client1 submitted the background job
        client1.interactions.create.assert_called_once()
        # client2 MUST NOT submit a duplicate job!
        client2.interactions.create.assert_not_called()
        # client1 closed upon disconnect
        client1.close.assert_called_once()
        # client2 resumed polling the exact same id
        client2.interactions.get.assert_called_once_with(id="test_interaction_jitter")

    @patch("time.sleep")
    def test_interactions_unsupported_falls_back_to_stream_with_thoughts(self, mock_sleep):
        """Test unsupported Interactions API seamlessly falls back to stream_with_thoughts with include_thoughts=True and filters thought chunks."""
        mock_client = MagicMock()
        # Interactions create raises unsupported error
        mock_client.interactions.create.side_effect = Exception("Interactions not supported for model gemini-4-carbon (404 Not Found)")

        # Generate stream with mixed thought and text chunks
        class FakePart:
            def __init__(self, text, thought):
                self.text = text
                self.thought = thought

        class FakeChunk:
            def __init__(self, parts, usage=None):
                self.candidates = [MagicMock(content=MagicMock(parts=parts))]
                self.text = None
                self.usage_metadata = usage

        chunks = [
            FakeChunk([FakePart("Thinking: Step 1 analyzing transcript...", thought=True)]),
            FakeChunk([FakePart('{"summary_zh": "流式思维回退成功", ', thought=False)]),
            FakeChunk([FakePart("Thinking: Step 2 synthesizing takeaways...", thought=True)]),
            FakeChunk(
                [FakePart('"key_takeaways_zh": ["回退要点"], "highlights": []}', thought=False)],
                usage=MagicMock(prompt_token_count=180, thoughts_token_count=450, candidates_token_count=90),
            ),
        ]
        mock_client.models.generate_content_stream.return_value = chunks

        result = generate_channel_a_structured_with_retry(
            client=mock_client,
            model_pool=["gemini-4-carbon"],
            user_prompt="Summarize this podcast",
            system_instruction="System prompt",
            response_schema=ChannelAOutput,
            temperature=0.2,
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=lambda: mock_client,
        )

        self.assertEqual(result.summary_zh, "流式思维回退成功")
        self.assertEqual(result.key_takeaways_zh, ["回退要点"])

        # Verify fallback stream configuration
        mock_client.models.generate_content_stream.assert_called_once()
        stream_call_args = mock_client.models.generate_content_stream.call_args[1]
        cfg = stream_call_args["config"]
        self.assertTrue(getattr(cfg.thinking_config, "include_thoughts", False))
        self.assertEqual(cfg.max_output_tokens, 65536)

    @patch("time.sleep")
    def test_non_thinking_400_triggers_fast_fallback_without_retry(self, mock_sleep):
        """Test non-thinking HTTP 400 error fast-falls back to stream_with_thoughts without wasting retries."""
        mock_client = MagicMock()
        # Simulate HTTP 400 with invalid field like response_mime_type in v1
        mock_client.interactions.create.side_effect = Exception(
            "400 - The field 'response_mime_type' is only available in v1beta"
        )

        class FakeChunk:
            def __init__(self, text):
                self.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(thought=False, text=text)]))]
                self.text = None
                self.usage_metadata = MagicMock(prompt_token_count=100, thoughts_token_count=50, candidates_token_count=50)

        mock_client.models.generate_content_stream.return_value = [
            FakeChunk('{"summary_zh": "400快速回退成功", "key_takeaways_zh": ["无需重试"], "highlights": []}')
        ]

        result = generate_channel_a_structured_with_retry(
            client=mock_client,
            model_pool=["gemini-4-carbon"],
            user_prompt="Summarize this podcast",
            system_instruction="System prompt",
            response_schema=ChannelAOutput,
            temperature=0.2,
            max_retries=5,
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=lambda: mock_client,
        )

        self.assertEqual(result.summary_zh, "400快速回退成功")
        # Ensure interactions.create was called only ONCE (did NOT retry 5 times!)
        self.assertEqual(mock_client.interactions.create.call_count, 1)
        # Ensure fallback stream was executed directly
        mock_client.models.generate_content_stream.assert_called_once()
        # Ensure no retry backoff delays were waited
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_interactions_incomplete_triggers_outer_candidate_retry(self, mock_sleep):
        """Test terminal 'incomplete' status causes model failure and triggers candidate retry to next model."""
        mock_client = MagicMock()

        # Model 1 (gemini-4-carbon) returns incomplete
        carbon_interaction = MagicMock(id="carbon_fail_id")
        carbon_poll = MagicMock(status="incomplete", errors=[{"message": "Max output tokens exceeded"}])

        # Model 2 (gemini-4-neon) succeeds
        neon_interaction = MagicMock(id="neon_ok_id")
        neon_poll = MagicMock(
            status="completed",
            output_text='{"summary_zh": "第二候选模型成功", "key_takeaways_zh": ["备用要点"], "highlights": []}',
            usage=MagicMock(total_input_tokens=200, total_thought_tokens=400, total_output_tokens=100),
        )

        def fake_create(*args, **kwargs):
            if kwargs.get("model") == "gemini-4-carbon":
                return carbon_interaction
            return neon_interaction

        def fake_get(id):
            if id == "carbon_fail_id":
                return carbon_poll
            return neon_poll

        mock_client.interactions.create.side_effect = fake_create
        mock_client.interactions.get.side_effect = fake_get

        result = generate_channel_a_structured_with_retry(
            client=mock_client,
            model_pool=["gemini-4-carbon", "gemini-4-neon"],
            user_prompt="Summarize this podcast",
            system_instruction="System prompt",
            response_schema=ChannelAOutput,
            temperature=0.2,
            max_retries=1,  # 1 attempt per model for fast test
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=lambda: mock_client,
        )

        self.assertEqual(result.summary_zh, "第二候选模型成功")
        self.assertEqual(result.key_takeaways_zh, ["备用要点"])

    @patch("time.sleep")
    @patch.dict("os.environ", {"CH_A_BG_TIMEOUT_SEC": "1.0"})
    def test_interactions_timeout_triggers_outer_retry(self, mock_sleep):
        """Test interaction polling exceeding timeout triggers outer retry and succeeds on next attempt."""
        mock_client = MagicMock()

        attempt_count = 0

        def fake_create(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            return MagicMock(id=f"timeout_id_{attempt_count}")

        mock_client.interactions.create.side_effect = fake_create

        # Attempt 1: poll advances time by 5.0s > 1.0s timeout -> TimeoutError
        # Attempt 2: poll advances time by 0.1s -> succeeds
        curr_time = 100.0

        def fake_time():
            nonlocal curr_time
            if attempt_count == 1:
                curr_time += 5.0
            else:
                curr_time += 0.1
            return curr_time

        def fake_get(id):
            if id == "timeout_id_1":
                return MagicMock(status="in_progress")
            return MagicMock(
                status="completed",
                output_text='{"summary_zh": "超时重试成功", "key_takeaways_zh": ["重试要点"], "highlights": []}',
                usage=MagicMock(total_input_tokens=100, total_thought_tokens=200, total_output_tokens=50),
            )

        mock_client.interactions.get.side_effect = fake_get

        with patch("time.time", side_effect=fake_time):
            result = generate_channel_a_structured_with_retry(
                client=mock_client,
                model_pool=["gemini-4-carbon"],
                user_prompt="Summarize",
                system_instruction="System",
                response_schema=ChannelAOutput,
                max_retries=2,
                show_id="TEST",
                client_factory=lambda: mock_client,
            )

        self.assertEqual(result.summary_zh, "超时重试成功")
        self.assertEqual(mock_client.interactions.create.call_count, 2)
        # Verify timed-out interaction was cancelled best-effort
        mock_client.interactions.cancel.assert_called_once_with(id="timeout_id_1")

    @patch("time.sleep")
    def test_interactions_thinking_error_steps_down_without_consuming_attempt(self, mock_sleep):
        """Test thinking_level rejection in interactions.create steps down within the same attempt (even max_retries=1)."""
        mock_client = MagicMock()
        seen_levels = []

        def fake_create(*args, **kwargs):
            lvl = kwargs["generation_config"].get("thinking_level")
            seen_levels.append(lvl)
            if lvl == "high":
                raise Exception("Unsupported thinking_level 'high' for this endpoint")
            return MagicMock(id="stepped_down_id")

        mock_client.interactions.create.side_effect = fake_create
        mock_client.interactions.get.return_value = MagicMock(
            status="completed",
            output_text='{"summary_zh": "降级成功", "key_takeaways_zh": ["要点1"], "highlights": []}',
            usage=MagicMock(total_input_tokens=100, total_thought_tokens=50, total_output_tokens=40),
        )

        result = generate_channel_a_structured_with_retry(
            client=mock_client,
            model_pool=["gemini-4-carbon"],
            user_prompt="Summarize",
            system_instruction="System",
            response_schema=ChannelAOutput,
            max_retries=1,
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=lambda: mock_client,
        )

        self.assertEqual(result.summary_zh, "降级成功")
        self.assertEqual(seen_levels, ["high", "low"])
        # Only the single 10s polling sleep occurred; zero retry backoff sleeps
        mock_sleep.assert_called_once_with(10)

    @patch("time.sleep")
    def test_interactions_requires_action_terminates_without_waiting_for_timeout(self, mock_sleep):
        """Test 'requires_action' status is treated as terminal error immediately."""
        mock_client = MagicMock()
        mock_client.interactions.create.return_value = MagicMock(id="req_action_id")
        mock_client.interactions.get.return_value = MagicMock(status="requires_action", errors=None)

        with self.assertRaises(RuntimeError) as ctx:
            generate_channel_a_structured_with_retry(
                client=mock_client,
                model_pool=["gemini-4-carbon"],
                user_prompt="Summarize",
                system_instruction="System",
                response_schema=ChannelAOutput,
                max_retries=1,
                show_id="TEST",
                client_factory=lambda: mock_client,
            )

        self.assertIn("requires_action", str(ctx.exception))
        self.assertEqual(mock_client.interactions.get.call_count, 1)

    @patch("time.sleep")
    def test_stream_fallback_degrades_remote_thinking_error(self, mock_sleep):
        """Test stream_with_thoughts fallback degrades remote thinking error within the same attempt."""
        mock_client = MagicMock(spec=["models", "close"])
        seen_levels = []

        class FakeChunk:
            def __init__(self, text):
                self.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(thought=False, text=text)]))]
                self.text = None
                self.usage_metadata = MagicMock(prompt_token_count=100, thoughts_token_count=20, candidates_token_count=30)

        def fake_stream(*args, **kwargs):
            cfg = kwargs["config"]
            th_cfg = getattr(cfg, "thinking_config", None)
            lvl = getattr(th_cfg, "thinking_level", None) if th_cfg else None
            lvl_str = (lvl.value if hasattr(lvl, "value") else str(lvl)).lower() if lvl is not None else None
            seen_levels.append(lvl_str)
            if lvl_str == "high":
                raise Exception("Invalid thinking_level parameter in generate_content_stream")
            return [FakeChunk('{"summary_zh": "流式降级成功", "key_takeaways_zh": ["要点"], "highlights": []}')]

        mock_client.models.generate_content_stream.side_effect = fake_stream

        result = generate_channel_a_structured_with_retry(
            client=mock_client,
            model_pool=["gemini-3.8-flash"],
            user_prompt="Summarize",
            system_instruction="System",
            response_schema=ChannelAOutput,
            max_retries=1,
            thinking_level_resolver=lambda m: "high",
            show_id="TEST",
            client_factory=lambda: mock_client,
        )

        self.assertEqual(result.summary_zh, "流式降级成功")
        self.assertEqual(seen_levels, ["high", "low"])

    def test_extract_interaction_text_steps_and_outputs_fallback(self):
        """Test extract_interaction_text handles output_text, steps (model_output), and legacy outputs."""
        # 1. steps fallback (dict shape)
        inter_steps = MagicMock(
            output_text="",
            steps=[
                {"type": "user_input", "content": [{"type": "text", "text": "prompt"}]},
                {"type": "thought", "content": [{"type": "text", "text": "thinking..."}]},
                {"type": "model_output", "content": [{"type": "text", "text": '{"a": 1}'}]},
            ],
            outputs=None,
        )
        self.assertEqual(extract_interaction_text(inter_steps), '{"a": 1}')

        # 2. legacy outputs fallback
        inter_outputs = MagicMock(
            output_text=None,
            steps=None,
            outputs=[MagicMock(text='{"b": 2}')],
        )
        self.assertEqual(extract_interaction_text(inter_outputs), '{"b": 2}')

    def test_ch_a_model_pool_and_default_thinking_levels(self):
        """Verify CH-A model pool and default high thinking policy across Carbon, Neon, and Flash."""
        self.assertEqual(DEFAULT_CH_A_POOL, ["gemini-4-carbon", "gemini-4-neon", "gemini-3.8-flash"])
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(resolve_ch_a_thinking_level("gemini-4-carbon", ""), "high")
            self.assertEqual(resolve_ch_a_thinking_level("gemini-4-neon", ""), "high")
            self.assertEqual(resolve_ch_a_thinking_level("gemini-3.8-flash", ""), "high")
            self.assertEqual(resolve_ch_a_thinking_level("gemini-4-neon", "xhigh"), "xhigh")


class TestCacheEdgeGuards(unittest.TestCase):
    """Test cache edge guards (P1-7): fresh audio_url preservation & empty transcript rejection."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_cache_edge_"))
        self.patcher = patch("pipeline.storage.CH_B_CACHE_DIR", self.test_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch("pipeline.runner.atomic_save_episode")
    @patch("pipeline.runner.run_channel_a")
    @patch("pipeline.hooks_tal.hook_tal_preprocess")
    def test_cache_hit_preserves_fresh_tal_archive_audio_url(self, mock_tal_hook, mock_ch_a, mock_save):
        guid = "tal_ep_436"
        save_ch_b_cache(
            show_id="tal",
            guid=guid,
            chunk_sec=180,
            duration_sec=3582,
            audio_url="https://stale-rss-ad-url.example.com/436.mp3",
            transcript=[{"id": 1, "start_sec": 0, "speaker": "Ira Glass", "en": "Hi", "zh": "你好"}],
        )
        mock_tal_hook.return_value = {
            "audio_url": "https://www.thisamericanlife.org/sites/default/files/audio/436/hash/436.mp3",
            "show_notes": "Clean acts",
            "speaker_roster": ["Ira Glass"],
        }
        mock_ch_a.return_value = {"summary_zh": "ok", "key_takeaways_zh": [], "highlights": [], "vocab_notes": [], "references": []}

        entry = {
            "id": guid,
            "guid": guid,
            "title": "436: The Psychopath Test",
            "published": "Mon, 23 Nov 2026 01:00:00 GMT",
            "enclosures": [{"href": "https://stale-rss-ad-url.example.com/436.mp3"}],
        }
        show = {"id": "tal", "title": "This American Life", "subtitle": "Narrative", "scraper_hook": "tal", "content_style": "narrative"}
        args = argparse.Namespace(chunk_sec=180, max_chunks=0, dry_run=False, no_cache=False)

        process_episode(entry, args, show, glossary_text="")

        saved_record = mock_save.call_args[0][1]
        self.assertEqual(
            saved_record["audio_url"],
            "https://www.thisamericanlife.org/sites/default/files/audio/436/hash/436.mp3",
        )

    @patch("pipeline.runner.run_channel_a")
    @patch("pipeline.runner.run_channel_b")
    @patch("pipeline.runner.chunk_audio")
    @patch("pipeline.runner.download_audio_file")
    @patch("pipeline.runner.get_audio_duration_sec")
    def test_empty_ch_b_transcript_aborts_before_cache_and_ch_a(
        self, mock_duration, mock_download, mock_chunk, mock_ch_b, mock_ch_a
    ):
        mock_duration.return_value = 600
        mock_file = MagicMock(spec=Path)
        mock_file.name = "chunk_000.mp3"
        mock_file.stat.return_value.st_size = 1024
        mock_chunk.return_value = [mock_file]
        mock_ch_b.return_value = []

        entry = {
            "id": "ep_empty",
            "guid": "ep_empty",
            "title": "Empty Transcript Ep",
            "published": "Mon, 23 Nov 2026 01:00:00 GMT",
            "enclosures": [{"href": "https://example.com/ep.mp3"}],
        }
        show = {"id": "airshow", "title": "The Air Show", "subtitle": "Aviation"}
        args = argparse.Namespace(chunk_sec=180, max_chunks=0, dry_run=False, no_cache=False)

        with self.assertRaises(RuntimeError):
            process_episode(entry, args, show, glossary_text="")

        mock_ch_a.assert_not_called()
        self.assertFalse(get_ch_b_cache_path("airshow", "ep_empty").exists())


class TestEnrichmentPipeline(unittest.TestCase):
    """Test --enrich incremental backfill pipeline (regression for P0-1 os.replace & idempotency)."""

    def setUp(self):
        self.temp_docs_data = Path(tempfile.mkdtemp(prefix="test_enrich_data_"))
        self.show_dir = self.temp_docs_data / "airshow"
        self.ep_dir = self.show_dir / "episodes"
        self.ep_dir.mkdir(parents=True, exist_ok=True)

        self.ep_file = self.ep_dir / "Buzzsprout-100.json"
        self.ep_doc = {
            "schema_version": "3.0",
            "show_id": "airshow",
            "guid": "Buzzsprout-100",
            "title": "Test Enrich Episode",
            "pub_date": "2026-11-20T00:00:00Z",
            "duration_sec": 1200,
            "audio_url": "https://example.com/100.mp3",
            "summary_zh": "原始摘要不可改变",
            "key_takeaways_zh": ["原始要点1"],
            "highlights": [],
            "transcript": [
                {"id": 1, "start_sec": 15, "speaker": "Jon Ostrower", "en": "Fifth-freedom flight.", "zh": "第五航权航班。"}
            ],
        }
        self.ep_file.write_text(json.dumps(self.ep_doc, ensure_ascii=False, indent=2), encoding="utf-8")

        index_doc = {
            "schema_version": "3.0",
            "show_id": "airshow",
            "podcast_title": "The Air Show",
            "rss_url": "https://example.com/rss",
            "updated_at": "2026-11-20T00:00:00Z",
            "episodes": [
                {
                    "guid": "Buzzsprout-100",
                    "title": "Test Enrich Episode",
                    "pub_date": "2026-11-20T00:00:00Z",
                    "duration_sec": 1200,
                    "audio_url": "https://example.com/100.mp3",
                    "file": "episodes/Buzzsprout-100.json",
                    "summary_zh": "原始摘要不可改变",
                    "key_takeaways_zh": ["原始要点1"],
                    "highlights_count": 0,
                    "transcript_count": 1,
                }
            ],
        }
        (self.show_dir / "index.json").write_text(json.dumps(index_doc, ensure_ascii=False, indent=2), encoding="utf-8")

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

    @patch("pipeline.runner.resolve_references_links", side_effect=lambda refs, **kw: [{**r, "links": []} for r in refs])
    @patch("pipeline.runner.generate_structured_with_retry")
    @patch("pipeline.runner.load_glossary_for_show", return_value="Glossary")
    @patch("pipeline.runner.fetch_rss_show_notes_map", return_value={"Buzzsprout-100": "Show notes"})
    @patch("pipeline.runner.create_genai_client")
    def test_enrich_writes_atomically_and_is_idempotent(
        self, mock_client_factory, mock_notes, mock_glossary, mock_gen, mock_links
    ):
        mock_client_factory.return_value = MagicMock()
        mock_gen.return_value = EnrichmentResponse(
            vocab_notes=[
                VocabNote(term="fifth-freedom flight", meaning_zh="第五航权航班", note_zh="航空术语", start_sec=15)
            ],
            references=[
                ReferenceItem(title="Flying Blind", creator="Peter Robison", category="book", note_zh="提及书目", start_sec=None)
            ],
        )

        args = argparse.Namespace(show="airshow", limit=0, dry_run=False)
        with patch("pipeline.runner.DOCS_DATA_DIR", self.temp_docs_data), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "fake_key"}):
            run_enrichment_pipeline(args, self.config)

        # Verify atomic write succeeded, no .tmp leftover, original fields preserved
        self.assertFalse(self.ep_file.with_suffix(".json.tmp").exists())
        updated = json.loads(self.ep_file.read_text(encoding="utf-8"))
        self.assertEqual(updated["summary_zh"], "原始摘要不可改变")
        self.assertEqual(len(updated["vocab_notes"]), 1)
        self.assertEqual(updated["vocab_notes"][0]["term"], "fifth-freedom flight")
        self.assertEqual(len(updated["references"]), 1)
        self.assertEqual(updated["references"][0]["title"], "Flying Blind")
        self.assertEqual(mock_gen.call_count, 1)

        # Re-running enrich should skip already-enriched episode
        with patch("pipeline.runner.DOCS_DATA_DIR", self.temp_docs_data), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "fake_key"}):
            run_enrichment_pipeline(args, self.config)
        self.assertEqual(mock_gen.call_count, 1)


class TestHooksTalAndRssIsolation(unittest.TestCase):
    """Test TAL scraper hooks (P1-8) and multi-show RSS fault isolation (P1-4)."""

    def test_extract_tal_episode_number_supports_title_page_and_archive_mp3(self):
        self.assertEqual(extract_tal_episode_number(title="436: The Psychopath Test"), 436)
        self.assertEqual(extract_tal_episode_number(link="https://www.thisamericanlife.org/850/some-slug"), 850)
        self.assertEqual(
            extract_tal_episode_number(
                link="https://www.thisamericanlife.org/sites/default/files/audio/436/skm6CBw0vYa/436.mp3"
            ),
            436,
        )
        self.assertIsNone(extract_tal_episode_number(title="Special Bonus Episode", link="https://example.com/audio.mp3"))

    def test_filter_tal_episodes_deduplicates_reruns_via_archive_url_and_in_feed(self):
        temp_data = Path(tempfile.mkdtemp(prefix="test_tal_filter_"))
        try:
            tal_dir = temp_data / "tal"
            tal_dir.mkdir(parents=True, exist_ok=True)
            index_doc = {
                "episodes": [
                    {
                        "guid": "old_guid_436",
                        "title": "Special Title Without Prefix",
                        "audio_url": "https://www.thisamericanlife.org/sites/default/files/audio/436/abc/436.mp3",
                    }
                ]
            }
            (tal_dir / "index.json").write_text(json.dumps(index_doc), encoding="utf-8")

            entries = [
                {"id": "new_rerun_guid", "title": "436: The Psychopath Test", "link": "https://www.thisamericanlife.org/436/psychopath"},
                {"id": "ep_851_a", "title": "851: Fresh Story", "link": "https://www.thisamericanlife.org/851/fresh"},
                {"id": "ep_851_dup", "title": "851: Fresh Story (Duplicate)", "link": "https://www.thisamericanlife.org/851/fresh"},
            ]
            show = {"id": "tal", "default_max_age_days": 0}
            with patch("pipeline.hooks_tal.DOCS_DATA_DIR", temp_data):
                selected = filter_tal_episodes(entries, show=show, limit=5, max_age_days=0)

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["id"], "ep_851_a")
        finally:
            shutil.rmtree(temp_data, ignore_errors=True)

    @patch("pipeline.hooks_tal.requests.get")
    def test_fetch_tal_episode_page_handles_null_archive_and_utf8_bytes(self, mock_get):
        html_bytes = (
            '<html><head><meta charset="utf-8"></head><body>'
            '<script id="playlist-data">{"archive": null}</script>'
            '<article class="node-act view-episode">'
            '<div class="field-name-field-act-label">Act One</div>'
            '<h2 class="act-header">Café Story</h2>'
            '<div class="field-name-body">By café reporter</div>'
            '<div class="byline"><a href="/archive?contributor=1">Dr. Jane Doe</a></div>'
            '</article></body></html>'
        ).encode("utf-8")
        mock_resp = MagicMock(status_code=200, content=html_bytes)
        mock_get.return_value = mock_resp

        archive_url, notes, acts = fetch_tal_episode_page("https://www.thisamericanlife.org/851/test")
        self.assertEqual(archive_url, "")
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["title"], "Café Story")
        self.assertEqual(acts[0]["reporters"], ["Jane Doe"])
        self.assertIn("### Act One: Café Story", notes)

    @patch("pipeline.hooks_tal.requests.get")
    def test_fetch_tal_transcript_page_scopes_to_main_and_handles_404(self, mock_get):
        html_bytes = (
            '<html><body>'
            '<nav><h4>Site Navigation</h4></nav>'
            '<main><article class="node-transcript">'
            '<h3>Prologue</h3><h4>Ira Glass</h4><h4>Dr. Robert Hare</h4>'
            '<h3>Subscribe to newsletter</h3>'
            '</article></main>'
            '<footer><h4>Footer Legal Heading</h4></footer>'
            '</body></html>'
        ).encode("utf-8")
        mock_get.return_value = MagicMock(status_code=200, content=html_bytes)
        roster, headings = fetch_tal_transcript_page(436)
        self.assertEqual(roster, ["Ira Glass", "Robert Hare"])
        self.assertEqual(headings, ["Prologue"])

        # 404 graceful fallback
        mock_get.return_value = MagicMock(status_code=404, content=b"Not Found")
        self.assertEqual(fetch_tal_transcript_page(999), ([], []))

    @patch("pipeline.runner.process_episode")
    @patch("pipeline.runner.filter_new_episodes")
    @patch("pipeline.runner.fetch_rss")
    @patch("pipeline.runner.load_glossary_for_show", return_value="Glossary")
    def test_run_main_pipeline_isolates_single_show_rss_failure(
        self, mock_glossary, mock_fetch_rss, mock_filter, mock_process
    ):
        def fake_fetch(url, show_id=""):
            if show_id == "airshow":
                raise RuntimeError("503 Service Unavailable")
            return MagicMock(entries=[{"id": "sinica_ep1", "title": "Sinica Ep 1"}])

        mock_fetch_rss.side_effect = fake_fetch
        mock_filter.return_value = [{"id": "sinica_ep1", "title": "Sinica Ep 1"}]

        args = argparse.Namespace(limit=1, max_age_days=None, chunk_sec=180, dry_run=False, max_chunks=0, no_cache=False)
        shows = [
            {"id": "airshow", "title": "The Air Show", "rss_url": "https://a.example/rss"},
            {"id": "sinica", "title": "Sinica Podcast", "rss_url": "https://b.example/rss"},
        ]

        attempted, failed = run_main_pipeline(args, {"shows": shows}, shows)
        self.assertEqual(attempted, 2)
        self.assertEqual(failed, 1)
        mock_process.assert_called_once()


class TestChannelBFreeTierRouting(unittest.TestCase):
    """Test Channel B Free-First + Paid-Overflow dual-key routing, 5 RPM guard, and 429 RPM/RPD classification."""

    def setUp(self):
        reset_ch_b_quota_state()

    def tearDown(self):
        reset_ch_b_quota_state()

    def test_config_defaults_and_dual_key_resolution(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "paid_k"}, clear=True):
            self.assertEqual(get_ch_b_api_keys(), ("", "paid_k"))
            self.assertEqual(get_ch_b_chunk_sec(), 300)
            self.assertEqual(get_ch_b_free_rpm(), 5)
            self.assertEqual(get_ch_b_free_rpd(), 20)
            self.assertTrue(get_ch_b_overflow_to_paid())

        # Same key for both -> treated as single-key Paid mode
        with patch.dict("os.environ", {"GEMINI_API_KEY": "same_k", "GEMINI_API_KEY_CH_B": "same_k"}, clear=True):
            self.assertEqual(get_ch_b_api_keys(), ("", "same_k"))

        # Distinct Free key + custom overrides
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_k",
                "GEMINI_API_KEY_CH_B": "free_k",
                "CH_B_CHUNK_SEC": "240",
                "CH_B_FREE_RPM": "4",
                "CH_B_FREE_RPD": "50",
                "CH_B_FREE_OVERFLOW_TO_PAID": "false",
            },
            clear=True,
        ):
            self.assertEqual(get_ch_b_api_keys(), ("free_k", "paid_k"))
            self.assertEqual(get_ch_b_chunk_sec(), 240)
            self.assertEqual(get_ch_b_free_rpm(), 4)
            self.assertEqual(get_ch_b_free_rpd(), 50)
            self.assertFalse(get_ch_b_overflow_to_paid())

    def test_rate_limit_classification_and_retry_delay_parsing(self):
        rpd_err = Exception(
            "429 RESOURCE_EXHAUSTED: Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )
        zero_limit_err = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded, limit: 0 for model gemini-4-hydrogen")
        rpm_err = Exception(
            "429 RESOURCE_EXHAUSTED: Quota exceeded for metric: "
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier. Please retry in 38.45s."
        )
        rpm_json_err = Exception("429 Too Many Requests {'retryDelay': '42s'} TokensPerMinute exceeded")
        auth_err = Exception("403 PERMISSION_DENIED: API_KEY_INVALID or Free tier unavailable")

        self.assertTrue(is_rate_limit_error(rpd_err))
        self.assertTrue(is_rpd_exhausted_error(rpd_err))
        self.assertTrue(is_rpd_exhausted_error(zero_limit_err))

        self.assertTrue(is_rate_limit_error(rpm_err))
        self.assertFalse(is_rpd_exhausted_error(rpm_err))
        self.assertAlmostEqual(parse_retry_delay_sec(rpm_err), 38.45, places=2)

        self.assertTrue(is_rate_limit_error(rpm_json_err))
        self.assertFalse(is_rpd_exhausted_error(rpm_json_err))
        self.assertAlmostEqual(parse_retry_delay_sec(rpm_json_err), 42.0, places=2)

        self.assertTrue(is_api_key_auth_error(auth_err))
        self.assertFalse(is_rate_limit_error(ValueError("Invalid JSON")))

    def test_sliding_window_rate_limiter_enforces_5_rpm_and_smooth_spacing(self):
        fake_now = [0.0]
        sleeps = []

        def fake_clock():
            return fake_now[0]

        def fake_sleep(sec):
            sleeps.append(sec)
            fake_now[0] += sec

        limiter = SlidingWindowRateLimiter(
            max_requests=5,
            window_sec=65.0,
            clock=fake_clock,
            sleeper=fake_sleep,
        )
        self.assertAlmostEqual(limiter.min_interval_sec, 12.4, places=2)

        # Call 1 at t=0.0 -> immediate
        self.assertEqual(limiter.acquire(), 0.0)
        # Call 2 at t=4.0 (4s after call 1) -> sleeps 8.4s to maintain 12.4s spacing (t=12.4)
        fake_now[0] += 4.0
        self.assertAlmostEqual(limiter.acquire(), 8.4, places=2)
        # Calls 3, 4, 5 each 4s later -> paced at 24.8, 37.2, 49.6
        for _ in range(3):
            fake_now[0] += 4.0
            limiter.acquire()
        self.assertAlmostEqual(fake_now[0], 49.6, places=2)

        # Call 6 arrives at t=53.6 -> rolling window has 5 entries starting at 0.0; must wait until t=65.0 (11.4s)
        fake_now[0] += 4.0
        slept_6 = limiter.acquire()
        self.assertAlmostEqual(slept_6, 11.4, places=2)
        self.assertAlmostEqual(fake_now[0], 65.0, places=2)

        # Call 7 arrives at t=85.0 (20s later, > 12.4s spacing and old entry popped) -> 0s sleep
        fake_now[0] += 20.0
        self.assertEqual(limiter.acquire(), 0.0)

    def _make_fake_chunk_file(self, name: str) -> Path:
        p = MagicMock(spec=Path)
        p.name = name
        p.__str__.return_value = f"/tmp/{name}"
        return p

    def _make_mock_client(self, tag: str, stream_side_effect):
        client = MagicMock()
        upload_counter = [0]

        def fake_upload(file):
            upload_counter[0] += 1
            return MagicMock(name=f"files/{tag}_{upload_counter[0]}")

        client.files.upload.side_effect = fake_upload
        client.files.get.side_effect = lambda name: MagicMock(name=name, state="FileState.ACTIVE")
        client.models.generate_content_stream.side_effect = stream_side_effect
        return client

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_free_first_happy_path_never_touches_paid(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:05", "speaker": "Jon Ostrower", "en": "Hello", "zh": "你好"}]}'
        free_client = self._make_mock_client("free", lambda **kw: [MagicMock(text=payload)])
        paid_client = self._make_mock_client("paid", lambda **kw: [MagicMock(text=payload)])

        created_keys = []

        def fake_factory(api_key=None):
            created_keys.append(api_key)
            return free_client if api_key == "free_key" else paid_client

        chunks = [self._make_fake_chunk_file("chunk_000.mp3"), self._make_fake_chunk_file("chunk_001.mp3")]
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_key",
                "GEMINI_API_KEY_CH_B": "free_key",
                "CH_B_FREE_RPM": "5",
                "CH_B_FREE_RPD": "20",
                "CHUNK_SLEEP_SEC": "0",
            },
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", side_effect=fake_factory):
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=600, show_id="airshow")

        self.assertEqual(len(transcript), 2)
        self.assertEqual(created_keys, ["free_key"])
        self.assertEqual(free_client.files.upload.call_count, 2)
        self.assertEqual(free_client.files.delete.call_count, 2)
        self.assertEqual(paid_client.files.upload.call_count, 0)
        self.assertEqual(CH_B_QUOTA_STATE.free_rpd_used.get("gemini-4-hydrogen"), 2)

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_rpd_429_immediately_overflows_to_paid_with_reupload(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:10", "speaker": "Kaiser Kuo", "en": "Welcome", "zh": "欢迎"}]}'
        paid_models_called = []

        def free_stream(**kw):
            raise Exception(
                "429 RESOURCE_EXHAUSTED: Quota exceeded for GenerateRequestsPerDayPerProjectPerModel-FreeTier"
            )

        def paid_stream(**kw):
            paid_models_called.append(kw["model"])
            return [MagicMock(text=payload)]

        free_client = self._make_mock_client("free", free_stream)
        paid_client = self._make_mock_client("paid", paid_stream)

        def fake_factory(api_key=None):
            return free_client if api_key == "free_key" else paid_client

        chunks = [self._make_fake_chunk_file("chunk_000.mp3"), self._make_fake_chunk_file("chunk_001.mp3")]
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_key",
                "GEMINI_API_KEY_CH_B": "free_key",
                "CH_B_FREE_RPM": "0",
                "CH_B_FREE_RPD": "20",
                "CH_B_FREE_OVERFLOW_TO_PAID": "true",
                "CHUNK_SLEEP_SEC": "0",
            },
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", side_effect=fake_factory):
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=600, show_id="sinica")

        self.assertEqual(len(transcript), 2)
        # Chunk 1 tried Free once (0 retries!), deleted Free file, then re-uploaded to Paid;
        # Chunk 2 saw Free RPD exhausted and went straight to Paid (NO second Free upload!)
        self.assertEqual(free_client.files.upload.call_count, 1)
        self.assertEqual(free_client.files.delete.call_count, 1)
        self.assertEqual(free_client.models.generate_content_stream.call_count, 1)
        self.assertEqual(paid_client.files.upload.call_count, 2)
        self.assertEqual(paid_client.files.delete.call_count, 2)
        self.assertEqual(paid_models_called, ["gemini-4-hydrogen", "gemini-4-hydrogen"])
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_proactive_rpd_budget_switches_to_paid(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:05", "speaker": "Ira Glass", "en": "Act one", "zh": "第一幕"}]}'
        free_client = self._make_mock_client("free", lambda **kw: [MagicMock(text=payload)])
        paid_client = self._make_mock_client("paid", lambda **kw: [MagicMock(text=payload)])

        def fake_factory(api_key=None):
            return free_client if api_key == "free_key" else paid_client

        chunks = [self._make_fake_chunk_file(f"chunk_00{i}.mp3") for i in range(3)]
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_key",
                "GEMINI_API_KEY_CH_B": "free_key",
                "CH_B_FREE_RPM": "0",
                "CH_B_FREE_RPD": "2",
                "CH_B_FREE_OVERFLOW_TO_PAID": "true",
                "CHUNK_SLEEP_SEC": "0",
            },
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", side_effect=fake_factory):
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=900, show_id="tal")

        self.assertEqual(len(transcript), 3)
        # First 2 chunks on Free, 3rd chunk routed straight to Paid without Free upload
        self.assertEqual(free_client.files.upload.call_count, 2)
        self.assertEqual(paid_client.files.upload.call_count, 1)

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_transient_rpm_429_retries_on_free_with_server_delay(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:05", "speaker": "Jon Ostrower", "en": "Ok", "zh": "好"}]}'
        attempts = [0]

        def free_stream(**kw):
            attempts[0] += 1
            if attempts[0] == 1:
                raise Exception(
                    "429 RESOURCE_EXHAUSTED: GenerateRequestsPerMinutePerProjectPerModel-FreeTier. Please retry in 25.0s."
                )
            return [MagicMock(text=payload)]

        free_client = self._make_mock_client("free", free_stream)
        paid_client = self._make_mock_client("paid", lambda **kw: [MagicMock(text=payload)])

        def fake_factory(api_key=None):
            return free_client if api_key == "free_key" else paid_client

        chunks = [self._make_fake_chunk_file("chunk_000.mp3")]
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_key",
                "GEMINI_API_KEY_CH_B": "free_key",
                "CH_B_FREE_RPM": "0",
                "CH_B_FREE_RPD": "20",
                "CHUNK_SLEEP_SEC": "0",
            },
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", side_effect=fake_factory):
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=300, show_id="airshow")

        self.assertEqual(len(transcript), 1)
        # Retried on Free Key (25.0 + 2.0 buffer = 27.0s) and never fell back to Paid
        self.assertEqual(attempts[0], 2)
        self.assertEqual(paid_client.files.upload.call_count, 0)
        mock_sleep.assert_any_call(27.0)

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_single_key_backward_compatible(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:05", "speaker": "Jon Ostrower", "en": "Hi", "zh": "嗨"}]}'
        paid_client = self._make_mock_client("paid", lambda **kw: [MagicMock(text=payload)])

        chunks = [self._make_fake_chunk_file("chunk_000.mp3"), self._make_fake_chunk_file("chunk_001.mp3")]
        with patch.dict(
            "os.environ",
            {"GEMINI_API_KEY": "paid_only_key", "CHUNK_SLEEP_SEC": "0"},
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", return_value=paid_client) as mock_factory:
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=600, show_id="airshow")

        self.assertEqual(len(transcript), 2)
        mock_factory.assert_called_once_with(api_key="paid_only_key")
        # Single-key Paid mode should not invoke Free Tier RPM guard sleeps
        mock_sleep.assert_not_called()

    def test_service_unavailable_error_detection(self):
        err_503 = Exception(
            "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.', 'status': 'UNAVAILABLE'}}"
        )
        err_429 = Exception("429 RESOURCE_EXHAUSTED: GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
        self.assertTrue(is_service_unavailable_error(err_503))
        self.assertFalse(is_service_unavailable_error(err_429))
        self.assertFalse(is_service_unavailable_error(ValueError("Malformed JSON")))

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_free_fast_3_retries_and_503_circuit_breaker(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:05", "speaker": "Jon Ostrower", "en": "Ok", "zh": "好"}]}'
        err_503 = Exception(
            "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.', 'status': 'UNAVAILABLE'}}"
        )

        free_client = self._make_mock_client("free", lambda **kw: (_ for _ in ()).throw(err_503))
        paid_client = self._make_mock_client("paid", lambda **kw: [MagicMock(text=payload)])

        def fake_factory(api_key=None):
            return free_client if api_key == "free_key" else paid_client

        chunks = [self._make_fake_chunk_file(f"chunk_00{i}.mp3") for i in range(3)]
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_key",
                "GEMINI_API_KEY_CH_B": "free_key",
                "CH_B_FREE_RPM": "0",
                "CH_B_FREE_RPD": "20",
                "CH_B_FREE_OVERFLOW_TO_PAID": "true",
                "CHUNK_SLEEP_SEC": "0",
            },
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", side_effect=fake_factory):
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=900, show_id="airshow")

        self.assertEqual(len(transcript), 3)
        # Chunk 1: 3 Free attempts (15s, 20s) -> Paid
        # Chunk 2: 3 Free attempts (15s, 20s) -> 2nd consecutive 503 trips circuit breaker -> Paid
        # Chunk 3: skips Free completely -> Paid directly!
        self.assertEqual(free_client.files.upload.call_count, 2)
        self.assertEqual(free_client.files.delete.call_count, 2)
        self.assertEqual(free_client.models.generate_content_stream.call_count, 6)
        self.assertEqual(paid_client.files.upload.call_count, 3)
        self.assertEqual(paid_client.files.delete.call_count, 3)
        self.assertIn("gemini-4-hydrogen", CH_B_QUOTA_STATE.free_503_tripped_models)
        # Verify fast-retry backoff sequence (only 15.0, 20.0 per chunk, never 25.0 or 30.0)
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertEqual(sleep_args, [15.0, 20.0, 15.0, 20.0])

    @patch("time.sleep")
    @patch("pipeline.gemini.get_audio_duration_sec", return_value=300)
    def test_run_channel_b_free_success_resets_consecutive_503_counter(self, mock_dur, mock_sleep):
        payload = '{"segments": [{"timestamp": "00:05", "speaker": "Jon Ostrower", "en": "Ok", "zh": "好"}]}'
        err_503 = Exception("503 UNAVAILABLE: high demand")
        free_upload_idx = [0]

        def fake_free_upload(file):
            free_upload_idx[0] += 1
            return MagicMock(name=f"files/free_{free_upload_idx[0]}")

        def free_stream(**kw):
            # Upload #1 (chunk 1) fails 503; Upload #2 (chunk 2) succeeds; Upload #3 (chunk 3) fails 503; Upload #4 (chunk 4) succeeds
            if free_upload_idx[0] in (1, 3):
                raise err_503
            return [MagicMock(text=payload)]

        free_client = MagicMock()
        free_client.files.upload.side_effect = fake_free_upload
        free_client.files.get.side_effect = lambda name: MagicMock(name=name, state="FileState.ACTIVE")
        free_client.models.generate_content_stream.side_effect = free_stream

        paid_client = self._make_mock_client("paid", lambda **kw: [MagicMock(text=payload)])

        def fake_factory(api_key=None):
            return free_client if api_key == "free_key" else paid_client

        chunks = [self._make_fake_chunk_file(f"chunk_00{i}.mp3") for i in range(4)]
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "paid_key",
                "GEMINI_API_KEY_CH_B": "free_key",
                "CH_B_FREE_RPM": "0",
                "CH_B_FREE_RPD": "20",
                "CH_B_FREE_OVERFLOW_TO_PAID": "true",
                "CHUNK_SLEEP_SEC": "0",
            },
            clear=True,
        ), patch("pipeline.gemini.create_genai_client", side_effect=fake_factory):
            transcript = run_channel_b(chunks=chunks, glossary_text="", duration_sec=1200, show_id="airshow")

        self.assertEqual(len(transcript), 4)
        # Circuit breaker MUST NOT have tripped because chunk 2 succeeded between chunk 1 and chunk 3 503s
        self.assertEqual(free_client.files.upload.call_count, 4)
        self.assertEqual(paid_client.files.upload.call_count, 2)
        self.assertNotIn("gemini-4-hydrogen", CH_B_QUOTA_STATE.free_503_tripped_models)
        self.assertEqual(CH_B_QUOTA_STATE.consecutive_503_chunks.get("gemini-4-hydrogen"), 0)


class TestShowsManifestIdempotency(unittest.TestCase):
    """Test sync_shows_manifest skips rewriting shows.json when content is unchanged."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_shows_sync_"))
        self.docs_data_dir = self.temp_dir / "docs" / "data"
        self.manifest_file = self.docs_data_dir / "shows.json"
        self.airshow_dir = self.docs_data_dir / "airshow"
        self.airshow_dir.mkdir(parents=True, exist_ok=True)
        (self.airshow_dir / "index.json").write_text(
            json.dumps(
                {
                    "schema_version": "3.0",
                    "show_id": "airshow",
                    "episodes": [{"guid": "ep-1", "pub_date": "2026-11-27T10:00:00Z"}],
                }
            ),
            encoding="utf-8",
        )
        self.mock_config = {
            "site_title": "Podcast Bilingual Reader",
            "shows": [
                {
                    "id": "airshow",
                    "title": "The Air Show",
                    "subtitle": "Aviation",
                    "accent_color": "#0a2540",
                    "enabled": True,
                }
            ],
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_shows_manifest_skips_rewrite_when_unchanged_and_updates_on_change(self):
        with patch("pipeline.storage.DOCS_DATA_DIR", self.docs_data_dir), \
             patch("pipeline.storage.SHOWS_MANIFEST_FILE", self.manifest_file), \
             patch("pipeline.storage.load_podcasts_config", return_value=self.mock_config):
            first = sync_shows_manifest()
            self.assertTrue(self.manifest_file.exists())
            self.assertEqual(first["shows"][0]["episode_count"], 1)

            # Force a known historic updated_at on disk to verify second sync does not rewrite it
            first["updated_at"] = "2026-11-20T00:00:00Z"
            self.manifest_file.write_text(json.dumps(first, ensure_ascii=False, indent=2), encoding="utf-8")
            before_bytes = self.manifest_file.read_bytes()

            second = sync_shows_manifest()
            self.assertEqual(second["updated_at"], "2026-11-20T00:00:00Z")
            self.assertEqual(self.manifest_file.read_bytes(), before_bytes)

            # Add a new episode -> manifest MUST update episode_count, latest_pub_date, and updated_at
            (self.airshow_dir / "index.json").write_text(
                json.dumps(
                    {
                        "schema_version": "3.0",
                        "show_id": "airshow",
                        "episodes": [
                            {"guid": "ep-2", "pub_date": "2026-11-28T00:00:00Z"},
                            {"guid": "ep-1", "pub_date": "2026-11-27T10:00:00Z"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            third = sync_shows_manifest()
            self.assertEqual(third["shows"][0]["episode_count"], 2)
            self.assertEqual(third["shows"][0]["latest_pub_date"], "2026-11-28T00:00:00Z")
            self.assertNotEqual(third["updated_at"], "2026-11-20T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
