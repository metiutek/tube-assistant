import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimeSafetyTests(unittest.TestCase):
    def test_authorized_chat_ids_are_fail_closed(self):
        from moduli import telegram_handler as tg

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tg._authorized_chat_ids(), set())

        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "123", "TELEGRAM_ALLOWED_CHAT_IDS": "456, 789"}):
            self.assertEqual(tg._authorized_chat_ids(), {"123", "456", "789"})

    def test_llm_channel_desc_update_executes_directly(self):
        from moduli import canale
        from moduli import telegram_handler as tg

        state = {}
        with patch.object(canale, "aggiorna_canale", return_value={"description": "aggiornata"}) as mock_upd:
            clean, added, saved, removed, results, deferred = tg._execute_actions(
                "Ok [AGGIORNA_DESC: nuova descrizione]",
                state,
            )

        self.assertEqual(clean, "Ok")
        self.assertEqual(added, [])
        self.assertEqual(saved, [])
        self.assertEqual(removed, [])
        self.assertEqual(deferred, [])
        self.assertEqual(len(results), 1)
        mock_upd.assert_called_once_with(description="nuova descrizione")
        self.assertNotIn("pending_actions", state)

    def test_llm_force_now_is_queued_for_confirmation(self):
        from moduli import telegram_handler as tg

        state = {}
        clean, added, saved, removed, results, deferred = tg._execute_actions(
            "Ok [FORZA_ORA: AI topic]",
            state,
        )

        self.assertEqual(clean, "Ok")
        self.assertEqual(added, [])
        self.assertEqual(deferred, [])
        self.assertEqual(len(results), 1)
        self.assertIn("pending_actions", state)
        self.assertEqual(state["pending_actions"][0]["type"], "FORZA_ORA")
        self.assertEqual(state["pending_actions"][0]["payload"], "AI topic")
        # nessun avvio diretto senza conferma esplicita
        self.assertNotIn("force_run", state)

    def test_confirmed_force_now_sets_immediate_publish(self):
        from moduli import telegram_handler as tg

        state = {}
        result = tg._execute_confirmed_action(
            state,
            {"type": "FORZA_ORA", "payload": "AI topic"},
        )

        self.assertIn("pipeline immediata", result)
        self.assertTrue(state["force_run"])
        self.assertTrue(state["publish_immediately"])
        self.assertEqual(state["topic_queue"], ["AI topic"])

    def test_pipeline_passes_immediate_to_upload(self):
        import importlib

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "123",
            "AI_SERVICE": "openrouter",
            "OPENROUTER_API_KEY": "key",
            "PEXELS_API_KEY": "pexels",
        }, clear=False):
            agent = importlib.import_module("agent")

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Path("state.json").write_text("{}", encoding="utf-8")
                calls = {}

                def fake_audio(_script, output_path):
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(output_path).write_bytes(b"audio")

                def fake_video(_audio, _keywords, _clips, output_path, **_kwargs):
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(output_path).write_bytes(b"video")

                def fake_thumb(_title, output_path, **_kwargs):
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(output_path).write_bytes(b"thumb")

                def fake_publish(*args, **kwargs):
                    calls["args"] = args
                    calls["kwargs"] = kwargs
                    return "video123"

                with patch.object(agent, "leggi_performance", return_value=[]), \
                    patch.object(agent, "calcola_strategia", return_value={}), \
                    patch.object(agent, "genera_topic", return_value="topic"), \
                    patch.object(agent, "genera_contenuto", return_value={
                        "title": "Title",
                        "description": "Desc",
                        "tags": ["tag"],
                        "script": "hello world",
                        "video_keywords": ["office"],
                    }), \
                    patch.object(agent, "genera_audio", side_effect=fake_audio), \
                    patch("moduli.asset.scarica_clips", return_value=["clip.mp4"]), \
                    patch.object(agent, "monta_video", side_effect=fake_video), \
                    patch.object(agent, "genera_thumbnail", side_effect=fake_thumb), \
                    patch.object(agent, "pubblica_video", side_effect=fake_publish), \
                    patch.object(agent, "notify_step"), \
                    patch.object(agent, "notify_start"), \
                    patch.object(agent, "notify_done"), \
                    patch.object(agent, "notify_error"), \
                    patch.object(agent, "notify_analytics"):
                    agent.run_pipeline({"publish_immediately": True})

                self.assertIsNone(calls["args"][3])
                self.assertTrue(calls["kwargs"]["immediate"])
                self.assertEqual(calls["kwargs"]["privacy_status"], "public")
            finally:
                os.chdir(old_cwd)

    def test_chat_falls_back_when_primary_provider_fails(self):
        """Regressione: la pipeline usa chat() — un provider primario giù non
        deve far perdere il video del giorno."""
        from moduli import ai_client

        with patch.object(ai_client, "_primary", side_effect=RuntimeError("down")), \
             patch.object(ai_client, "_fallback", return_value="ok") as mock_fb:
            self.assertEqual(ai_client.chat("ciao"), "ok")
        mock_fb.assert_called_once()

    def test_fallback_skips_openrouter_without_key(self):
        from moduli import ai_client

        with patch.dict(os.environ, {"AI_SERVICE": "ollama_cloud", "OPENROUTER_API_KEY": ""}), \
             patch.object(ai_client, "_openrouter") as mock_or, \
             patch.object(ai_client, "_ollama_local", return_value="local") as mock_local:
            self.assertEqual(ai_client._fallback([{"role": "user", "content": "x"}]), "local")
        mock_or.assert_not_called()
        mock_local.assert_called_once()

    def test_short_script_is_retried_then_rejected(self):
        """Regressione: script da poche parole = video di pochi secondi —
        mai pubblicarlo."""
        import json as _json
        from moduli import cervello

        short = _json.dumps({
            "title": "Topic explained", "description": "About topic", "tags": ["topic"],
            "script": "too short", "video_keywords": ["office"],
        })
        # durata default 8 min * 150 parole/min = 1200 parole di target
        good = _json.dumps({
            "title": "Topic explained", "description": "About topic", "tags": ["topic"],
            "script": "word " * 1200, "video_keywords": ["office"],
        })

        # primo tentativo corto: si chiede una CONTINUAZIONE (prosa, non JSON)
        # invece di rigenerare tutto — e con quella si arriva a target
        continuazione = "parola " * 1200
        with patch.object(cervello, "chat_ollama",
                          side_effect=[short, continuazione]) as mock_chat:
            content = cervello.genera_contenuto("topic")
        self.assertEqual(mock_chat.call_count, 2)
        self.assertGreaterEqual(len(content["script"].split()), 960)

        # sempre corto e nemmeno la continuazione aiuta → ValueError dopo
        # TENTATIVI_CONTENUTO generazioni complete
        inutile = "niente"
        with patch.object(cervello, "chat_ollama",
                          side_effect=[short, inutile] * cervello.TENTATIVI_CONTENUTO):
            with self.assertRaises(ValueError):
                cervello.genera_contenuto("topic")

    def test_script_sotto_target_ma_usabile_viene_accettato(self):
        """Gli LLM contano male le parole e stanno quasi sempre sotto il
        target: dopo i tentativi si tiene lo script piu' lungo ottenuto,
        purche' sopra la soglia minima — meglio un video corto che nessuno."""
        import json as _json
        from moduli import cervello

        def script(n):
            return _json.dumps({
                "title": "Topic explained", "description": "About topic", "tags": ["topic"],
                "script": "word " * n, "video_keywords": ["office"],
            })

        # target 8 min * 150 = 1200 parole; soglia buona 960, minima 600.
        # "" = continuazione inutilizzabile, cosi' nessun tentativo arriva a
        # target e si verifica davvero il ripiego sul piu' lungo.
        risposte = [script(620), "", script(700), "", script(650), ""]
        with patch.object(cervello, "chat_ollama", side_effect=risposte) as mock:
            content = cervello.genera_contenuto("topic")
        self.assertEqual(mock.call_count, len(risposte))
        # tiene il piu' lungo dei tre, non il primo
        self.assertEqual(len(content["script"].split()), 700)

    def test_common_errors_get_human_explanation(self):
        import importlib

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "123",
            "AI_SERVICE": "openrouter",
            "OPENROUTER_API_KEY": "key",
            "PEXELS_API_KEY": "pexels",
        }, clear=False):
            agent = importlib.import_module("agent")

        casi = {
            'HttpError 403 "uploadLimitExceeded"': "Quota YouTube",
            "RefreshError: invalid_grant": "token.json",
            "OSError: [Errno 28] No space left on device": "Disco pieno",
            "RuntimeError: No Pexels results for: robot": "Nessuna clip",
            "HTTPError: 429 Too Many Requests": "rate limit",
            "ffmpeg not found. Install it": "FFMPEG_PATH",
            "ValueError: Script troppo corto: 80 parole": "script troppo corto",
        }
        for errore, atteso in casi.items():
            spiegazione = agent._spiega_errore(errore)
            self.assertIn(atteso.lower(), spiegazione.lower(),
                          f"errore non spiegato: {errore!r} -> {spiegazione!r}")
        # errore sconosciuto → stringa vuota (si usa il messaggio generico)
        self.assertEqual(agent._spiega_errore("KeyError: 'boh'"), "")

    def test_srt_covers_audio_duration_with_monotonic_timing(self):
        from moduli.sottotitoli import genera_srt, _blocchi

        script = ("First sentence here. " * 3 +
                  "A much longer sentence with many many words that gets split into blocks of twelve words maximum right here. " * 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "subs.srt")
            self.assertEqual(genera_srt(script, 60.0, out), out)
            text = Path(out).read_text(encoding="utf-8")

        # blocchi max 12 parole
        for blocco in _blocchi(script):
            self.assertLessEqual(len(blocco.split()), 12)
        # formato e copertura: ultimo timestamp == durata audio
        self.assertIn("00:00:00,000 -->", text)
        self.assertIn("00:01:00,000", text)
        # timing monotono
        import re as _re
        times = [int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
                 for h, m, s, ms in _re.findall(r"(\d+):(\d+):(\d+),(\d+)", text)]
        self.assertEqual(times, sorted(times))

        # input vuoto → None, nessun file
        self.assertIsNone(genera_srt("", 60.0, "unused.srt"))
        self.assertIsNone(genera_srt("ciao mondo", 0, "unused.srt"))

    def test_short_pexels_clips_are_filtered_out(self):
        from moduli.asset import _filtra_durata

        videos = [{"id": 1, "duration": 3}, {"id": 2, "duration": 12}, {"id": 3}]
        self.assertEqual(_filtra_durata(videos), [{"id": 2, "duration": 12}])
        # se nessuna clip è abbastanza lunga, meglio le corte di niente
        corte = [{"id": 1, "duration": 2}, {"id": 3, "duration": 4}]
        self.assertEqual(_filtra_durata(corte), corte)

    def test_model_json_parser_uses_first_complete_object(self):
        from moduli.cervello import _parse_json

        data = _parse_json(
            'intro {"title":"T","description":"D","tags":["a"],'
            '"script":"S","video_keywords":["office"]} trailing {"bad": true}'
        )

        self.assertEqual(data["title"], "T")
        self.assertEqual(data["video_keywords"], ["office"])

    def test_trigger_hours_derive_from_publish_hours(self):
        """Regressione: /setpubblica scrive publish_hours_utc — il daemon deve
        produrre 3 ore prima, non restare sul default delle 14 UTC."""
        import importlib

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "123",
            "AI_SERVICE": "openrouter",
            "OPENROUTER_API_KEY": "key",
            "PEXELS_API_KEY": "pexels",
        }, clear=False):
            agent = importlib.import_module("agent")

        # orari di pubblicazione manuali → trigger = publish - 3
        state = {"videos_per_day": 2, "publish_hours_utc": [12, 20]}
        self.assertEqual(agent._get_trigger_hours(state), [9, 17])

        # auto-scheduling: best_hours_utc sono orari di pubblicazione
        state = {"videos_per_day": 1, "auto_scheduling": True, "best_hours_utc": [2]}
        self.assertEqual(agent._get_trigger_hours(state), [23])

        # trigger espliciti (hand-edit) vincono su tutto
        state = {"videos_per_day": 1, "trigger_hours_utc": [6], "publish_hours_utc": [20]}
        self.assertEqual(agent._get_trigger_hours(state), [6])

        # nessuna configurazione → default
        self.assertEqual(agent._get_trigger_hours({}), [14])

    def test_pipeline_persists_topic_queue_pop(self):
        """Regressione: il topic preso dalla coda deve sparire da state.json,
        altrimenti viene riprodotto identico a ogni run."""
        import importlib
        import json as _json

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "123",
            "AI_SERVICE": "openrouter",
            "OPENROUTER_API_KEY": "key",
            "PEXELS_API_KEY": "pexels",
        }, clear=False):
            agent = importlib.import_module("agent")

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Path("state.json").write_text(
                    _json.dumps({"topic_queue": ["Topic A", "Topic B"]}), encoding="utf-8"
                )

                def fake_file(_a, output_path, **_kw):
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(output_path).write_bytes(b"x")

                def fake_video(_audio, _keywords, _clips, output_path, **_kw):
                    fake_file(None, output_path)

                with patch.object(agent, "leggi_performance", return_value=[]), \
                    patch.object(agent, "calcola_strategia", return_value={}), \
                    patch.object(agent, "genera_contenuto", return_value={
                        "title": "Title", "description": "Desc", "tags": ["tag"],
                        "script": "hello world", "video_keywords": ["office"],
                    }), \
                    patch.object(agent, "genera_audio", side_effect=fake_file), \
                    patch("moduli.asset.scarica_clips", return_value=["clip.mp4"]), \
                    patch.object(agent, "monta_video", side_effect=fake_video), \
                    patch.object(agent, "genera_thumbnail", side_effect=fake_file), \
                    patch.object(agent, "pubblica_video", return_value="vid1"), \
                    patch.object(agent, "notify_step"), patch.object(agent, "notify_start"), \
                    patch.object(agent, "notify_done"), patch.object(agent, "notify_error"), \
                    patch.object(agent, "notify_analytics"):
                    agent.run_pipeline(agent._load_state())

                saved = _json.loads(Path("state.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["topic_queue"], ["Topic B"])
                self.assertIn("Topic A", saved["recent_topics"])
                # checkpoint e status ripuliti a fine run
                self.assertFalse(Path("output/pipeline_checkpoint.json").exists())
                self.assertNotIn("pipeline_status", saved)
            finally:
                os.chdir(old_cwd)

    def test_autoscheduling_uses_best_performance_hours(self):
        import importlib

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "123",
            "AI_SERVICE": "openrouter",
            "OPENROUTER_API_KEY": "key",
            "PEXELS_API_KEY": "pexels",
        }, clear=False):
            agent = importlib.import_module("agent")
        state = {"videos_per_day": 2}
        agent._update_best_hours(state, [
            {"published_hour_utc": 8, "views": 10, "ctr_percent": 1, "duration_seconds": 100, "avg_view_duration_seconds": 20},
            {"published_hour_utc": 20, "views": 1000, "ctr_percent": 8, "duration_seconds": 100, "avg_view_duration_seconds": 70},
            {"published_hour_utc": 14, "views": 700, "ctr_percent": 6, "duration_seconds": 100, "avg_view_duration_seconds": 50},
        ])

        self.assertEqual(state["best_hours_utc"], [14, 20])


if __name__ == "__main__":
    unittest.main()
