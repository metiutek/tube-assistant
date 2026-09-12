"""Regressioni emerse dai test end-to-end con servizi reali.

Sono tutti bug che i test unitari non potevano vedere perche' si manifestano
solo eseguendo davvero la pipeline fuori dalla cartella del sorgente, con un
modello reasoning, o con la risposta vera di un servizio esterno.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TtsSubprocessTests(unittest.TestCase):
    def test_worker_importabile_da_qualsiasi_cwd(self):
        """Regressione: il subprocess TTS faceva `sys.path.insert(0, '.')` e
        quindi trovava `moduli` solo se lanciato dalla root del sorgente.
        Installato come tool con una workspace separata — lo scenario del
        README — ogni chunk falliva con ModuleNotFoundError e la pipeline
        ripiegava in silenzio su gTTS, ignorando la preferenza `tts_voce`."""
        from moduli import audio

        self.assertTrue(os.path.isdir(os.path.join(audio._RADICE_PROGETTO, "moduli")),
                        audio._RADICE_PROGETTO)
        with tempfile.TemporaryDirectory() as estraneo:
            res = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {audio._RADICE_PROGETTO!r});"
                 "from moduli.audio import _tts_worker; print('ok')"],
                cwd=estraneo, capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(res.returncode, 0, res.stderr[-400:])

    def test_log_clip_non_esplode_su_console_ascii(self):
        """Regressione: i messaggi con frecce sollevavano UnicodeEncodeError
        su Windows quando stdout non e' UTF-8, interrompendo il download."""
        from moduli.asset import _stampa

        class StdoutAscii:
            encoding = "cp1252"

            def write(self, s):
                s.encode("cp1252")  # come il vero stream: solleva sui non mappabili
                return len(s)

            def flush(self):
                pass

        with patch.object(sys, "stdout", StdoutAscii()):
            _stampa("  → 5 clip ✓ scaricate ✗")  # non deve sollevare


class RisoluzioneClipTests(unittest.TestCase):
    def test_sceglie_la_risoluzione_minima_sufficiente(self):
        """Regressione: si scaricava sempre il file piu' grande. Per un output
        1080p arrivavano master 4K da centinaia di MB (misurato: 926MB di
        cache per un solo video, con clip scartate perche' sopra il limite)."""
        from moduli.asset import _best_file

        files = [
            {"width": 640, "link": "sd"},
            {"width": 1280, "link": "hd"},
            {"width": 1920, "link": "fullhd"},
            {"width": 3840, "link": "uhd"},
        ]
        with patch.dict(os.environ, {"VIDEO_WIDTH": "1920"}):
            self.assertEqual(_best_file(list(files)), "fullhd")
        with patch.dict(os.environ, {"VIDEO_WIDTH": "1280"}):
            self.assertEqual(_best_file(list(files)), "hd")

    def test_ripiega_sulla_massima_se_nessuna_basta(self):
        from moduli.asset import _best_file

        files = [{"width": 640, "link": "sd"}, {"width": 854, "link": "meglio"}]
        with patch.dict(os.environ, {"VIDEO_WIDTH": "1920"}):
            self.assertEqual(_best_file(files), "meglio")

    def test_file_senza_link_scartati(self):
        from moduli.asset import _best_file

        with self.assertRaises(RuntimeError):
            _best_file([{"width": 1920}, {"width": 3840, "link": None}])


class OllamaTests(unittest.TestCase):
    def test_ragionamento_non_viene_spacciato_per_risposta(self):
        """Regressione misurata in produzione: nemotron/gpt-oss riempiono
        `thinking` e lasciano `content` vuoto. Il vecchio `content or thinking`
        faceva finire il monologo del modello ("We need to output a short
        phrase 3-7 words...") dritto nel titolo del video — 2 topic su 5."""
        from moduli.ai_client import RispostaVuota, _testo_ollama

        self.assertEqual(_testo_ollama({"message": {"content": "ciao"}}, "m"), "ciao")
        with self.assertRaises(RispostaVuota):
            _testo_ollama({"message": {"content": "", "thinking": "We need to..."}}, "m")
        with self.assertRaises(RispostaVuota):
            _testo_ollama({"message": {"content": ""}, "done_reason": "length"}, "qwen")

    def test_ritenta_con_piu_token_se_manca_il_contenuto(self):
        """Il budget consumato dal ragionamento va allargato, non ignorato."""
        from moduli import ai_client

        budget = []

        def finta_post(url, headers=None, json=None, timeout=None):
            budget.append(json["options"]["num_predict"])
            risposta = MagicMock(status_code=200)
            risposta.json.return_value = (
                {"message": {"content": "", "thinking": "penso"}}
                if len(budget) == 1 else {"message": {"content": "risposta vera"}})
            return risposta

        with patch.object(ai_client.requests, "post", side_effect=finta_post):
            testo = ai_client._ollama_local([{"role": "user", "content": "x"}],
                                            max_tokens=64)
        self.assertEqual(testo, "risposta vera")
        self.assertEqual(len(budget), 2)
        self.assertGreater(budget[1], budget[0])

    def test_topic_che_e_un_ragionamento_viene_scartato(self):
        from moduli.cervello import _topic_non_valido

        self.assertTrue(_topic_non_valido(
            "We need to output a short phrase 3-7 words, no punctuation", set()))
        self.assertTrue(_topic_non_valido("parola " * 30, set()))
        self.assertTrue(_topic_non_valido("", set()))
        self.assertTrue(_topic_non_valido("Gia Usato", {"gia usato"}))
        self.assertFalse(_topic_non_valido("AI Chips Are Getting Smaller", set()))

    def test_ritmo_di_lettura_realistico(self):
        """Misurato su due script veri: 147 e 159 parole/minuto. Con la stima
        precedente (130) i video uscivano ~18% piu' corti del richiesto."""
        from moduli.cervello import _parole_al_minuto

        self.assertGreaterEqual(_parole_al_minuto(), 140)
        with patch.dict(os.environ, {"PAROLE_AL_MINUTO": "170"}):
            self.assertEqual(_parole_al_minuto(), 170)

    def test_modello_mancante_da_errore_comprensibile(self):
        """Un 404 da Ollama significa quasi sempre "modello non scaricato",
        ma arrivava all'utente come nudo `404 Client Error`."""
        from moduli import ai_client

        risposta = MagicMock(status_code=404)
        with patch.object(ai_client.requests, "post", return_value=risposta):
            with self.assertRaises(RuntimeError) as ctx:
                ai_client._ollama_local([{"role": "user", "content": "x"}])
        self.assertIn("ollama pull", str(ctx.exception))

    def test_modello_cloud_configurabile_da_env(self):
        """Era l'unico provider col modello cablato nel sorgente."""
        from moduli import ai_client

        visti = []

        def finta_post(url, headers=None, json=None, timeout=None):
            visti.append(json["model"])
            r = MagicMock(status_code=200)
            r.json.return_value = {"message": {"content": "ok"}}
            return r

        with patch.dict(os.environ, {"OLLAMA_API_KEY": "k",
                                     "OLLAMA_CLOUD_MODEL": "gemma4:31b"}), \
             patch.object(ai_client.requests, "post", side_effect=finta_post):
            ai_client._ollama_cloud([{"role": "user", "content": "x"}])
        self.assertEqual(visti, ["gemma4:31b"])

    def test_chat_non_restituisce_risposta_vuota(self):
        from moduli import ai_client

        with patch.object(ai_client, "_primary", return_value="   "), \
             patch.object(ai_client, "_fallback", return_value="vero") as fb:
            self.assertEqual(ai_client.chat("x"), "vero")
        fb.assert_called_once()


class StateOstileTests(unittest.TestCase):
    def test_state_non_dict_non_rompe_il_daemon(self):
        """Regressione: `state.json` e' una superficie di controllo editabile a
        mano. Con `[...]` o `null` load_state restituiva list/None e il primo
        `state.get(...)` sollevava AttributeError: il daemon non partiva e
        l'errore non diceva quale fosse il problema."""
        from moduli import state_io

        with tempfile.TemporaryDirectory() as tmp:
            vecchio = os.getcwd()
            os.chdir(tmp)
            try:
                for contenuto in ("[1,2,3]", "null", "42", '"testo"', "{rotto", ""):
                    Path("state.json").write_text(contenuto, encoding="utf-8")
                    stato = state_io.load_state()
                    self.assertIsInstance(stato, dict, contenuto)
                    stato.get("qualsiasi")  # non deve sollevare
            finally:
                os.chdir(vecchio)

    def test_backup_recupera_stato_valido(self):
        """Il .bak conserva la versione PRECEDENTE (viene scritto solo dal
        secondo salvataggio in poi): se state.json si corrompe si torna li',
        invece di ripartire da zero perdendo coda e cronologia."""
        from moduli import state_io

        with tempfile.TemporaryDirectory() as tmp:
            vecchio = os.getcwd()
            os.chdir(tmp)
            try:
                state_io.save_state({"topic_queue": ["primo"]})
                state_io.save_state({"topic_queue": ["secondo"]})
                self.assertTrue(Path("state.json.bak").exists())
                Path("state.json").write_text("[corrotto", encoding="utf-8")
                self.assertEqual(state_io.load_state(), {"topic_queue": ["primo"]})
            finally:
                os.chdir(vecchio)


class ThumbnailPromptTests(unittest.TestCase):
    def test_pollinations_riceve_la_descrizione_dettagliata(self):
        """Regressione: pollinations e' il provider di DEFAULT e riceveva solo
        il titolo, ricostruendosi un prompt generico. Il `thumbnail_description`
        dell'LLM — per cui cervello.py dedica un intero paragrafo di istruzioni
        — veniva scartato, mentre il log lo stampava lo stesso."""
        from moduli import thumbnail

        descrizione = ("Close-up of a glowing titanium robot face in a dark server "
                       "room, cyan rim light, volumetric fog, 85mm lens")
        visti = []

        def finta_get(url, **kw):
            visti.append(url)
            raise RuntimeError("stop dopo aver catturato l'url")

        with patch.object(thumbnail.requests, "get", side_effect=finta_get):
            try:
                thumbnail._fetch_image_pollinations(descrizione, mood="epic")
            except RuntimeError:
                pass
        self.assertTrue(visti)
        self.assertIn("titanium", visti[0])
        self.assertIn("volumetric", visti[0])

    def test_preset_font_limitato_dal_numero_di_parole(self):
        """L'LLM sceglie il preset e sbaglia spesso (chiede "B" per tre parole
        contro le sue stesse regole), riempiendo meta' copertina di testo."""
        from moduli.thumbnail import _preset_sensato

        self.assertEqual(_preset_sensato("A", "AI GOES LOCAL"), "C")
        self.assertEqual(_preset_sensato("A", "AI WINS"), "B")
        self.assertEqual(_preset_sensato("A", "NOW"), "A")
        self.assertEqual(_preset_sensato("A", "UNA FRASE MOLTO LUNGA"), "D")
        self.assertEqual(_preset_sensato("D", "AI GOES LOCAL"), "D")  # piu' piccolo: ok
        self.assertEqual(_preset_sensato("", "X Y Z"), "C")
        self.assertEqual(_preset_sensato("Z", "X Y Z"), "C")  # preset inventato


class FitTitoloTests(unittest.TestCase):
    def test_preferisce_una_riga_sola(self):
        """Regressione: si cercava la dimensione piu' grande che entrasse in
        tre righe, quindi il titolo veniva spezzato su due righe enormi che
        occupavano meta' copertina anche quando bastava un font poco piu'
        piccolo per tenerlo su una riga."""
        from moduli.thumbnail import _fit_title, THUMB_W, THUMB_H

        _font, righe, size = _fit_title("AI GOES LOCAL", int(THUMB_W * 0.86),
                                        int(THUMB_H * 0.55), max_start=160)
        self.assertEqual(len(righe), 1, righe)
        self.assertGreaterEqual(size, 90)

    def test_titolo_lungo_va_a_capo_senza_tagliare(self):
        from moduli.thumbnail import _fit_title, THUMB_W, THUMB_H

        titolo = "THE COMPLETE FUTURE OF ARTIFICIAL INTELLIGENCE EXPLAINED"
        _font, righe, _size = _fit_title(titolo, int(THUMB_W * 0.86),
                                         int(THUMB_H * 0.55), max_start=200)
        self.assertLessEqual(len(righe), 3)
        # nessuna parola persa nel wrapping
        self.assertEqual(" ".join(righe).split(), titolo.split())


class CaptionRifinituraTests(unittest.TestCase):
    def test_caption_non_finisce_su_parola_funzionale(self):
        """Una caption troncata a meta' su "FOR THE" resta a mezz'aria."""
        from moduli.montaggio import _caption_text

        testo = _caption_text(
            "That is a fifty percent reduction in cost for the same capability.")
        self.assertFalse(testo.split()[-1] in {"FOR", "THE", "IN", "A"}, testo)
        self.assertTrue(testo.endswith("COST"), testo)

    def test_frase_corta_non_viene_svuotata(self):
        from moduli.montaggio import _caption_text

        self.assertTrue(_caption_text("Breve frase."))


class SetpubblicaTests(unittest.TestCase):
    def test_avvisa_se_gli_orari_superano_i_video_al_giorno(self):
        """Regressione: `/setpubblica 12 20` con videos_per_day=1 applicava un
        solo orario senza spiegare perche' il secondo sparisse."""
        import asyncio
        from moduli import telegram_handler as tg

        with tempfile.TemporaryDirectory() as tmp:
            vecchio = os.getcwd()
            os.chdir(tmp)
            try:
                Path("state.json").write_text('{"videos_per_day": 1}', encoding="utf-8")
                inviati = []
                msg = MagicMock()

                async def reply(t, **kw):
                    inviati.append(t)

                msg.reply_text = reply
                upd = MagicMock()
                upd.message = msg
                ctx = MagicMock()
                ctx.args = ["12", "20"]
                asyncio.run(tg.cmd_setpubblica(upd, ctx))
                self.assertTrue(inviati)
                self.assertIn("setvideogiorno", inviati[-1])
            finally:
                os.chdir(vecchio)


if __name__ == "__main__":
    unittest.main()


class UsoDaUtenteTests(unittest.TestCase):
    """Bug emersi usando il progetto dalla riga di comando come farebbe un utente."""

    def test_contenuto_deve_parlare_del_topic_richiesto(self):
        """Regressione: la strategia auto-appresa dalle analytics imponeva un
        title_style letterale e il modello riscriveva il video su un altro
        argomento — topic "L'evoluzione degli LLM" -> titolo "He Struck a
        110mph Tennis Serve Blind". Il topic scelto dall'utente deve vincere."""
        from moduli.cervello import _contenuto_fuori_tema

        topic = "L'evoluzione degli LLM: da GPT-1 ai modelli di oggi"
        fuori = {"title": "He Struck a 110mph Tennis Serve Blind",
                 "description": "tennis reflexes", "tags": ["tennis"]}
        self.assertTrue(_contenuto_fuori_tema(topic, fuori))

        # il canale scrive i topic in italiano e genera i video in inglese:
        # il confronto deve reggere il cambio di lingua
        dentro_en = {"title": "The Evolution of LLMs: From GPT-1 to Today",
                     "description": "language models", "tags": ["llm"]}
        dentro_it = {"title": "Come si sono evoluti gli LLM", "description": "",
                     "tags": []}
        self.assertFalse(_contenuto_fuori_tema(topic, dentro_en))
        self.assertFalse(_contenuto_fuori_tema(topic, dentro_it))
        # tag e keyword NON bastano: il modello ci infila le parole del topic
        # anche quando ha scritto tutt'altro (visto in produzione)
        self.assertTrue(_contenuto_fuori_tema(topic, {
            "title": "He Hit a 180mph Fastball Blindfolded",
            "script": "baseball bat speed reflexes " * 40,
            "tags": ["llm", "gpt"], "video_keywords": ["gpt-1 model"]}))
        # titolo che non nomina la sigla va bene se lo script parla davvero del tema
        self.assertFalse(_contenuto_fuori_tema(topic, {
            "title": "The Evolution of Language Models",
            "script": "llm history gpt models llm " * 20}))

        # topic senza sigle/nomi propri: non si giudica, per non bloccare
        # la pipeline con un falso positivo
        self.assertFalse(_contenuto_fuori_tema("come funzionano i motori elettrici",
                                               {"title": "Anything at all"}))

    def test_one_shot_e_un_delegato_non_una_copia(self):
        """Regressione: `youtube_ai_agent/_one_shot.py` era una TERZA copia
        della pipeline, rimasta indietro (vecchio `scarica_clip`, nessuna
        protezione della cache, coda topic ignorata) — ed e' proprio quella
        che esegue `tube-assistant run` / `dry-run`."""
        import inspect
        from youtube_ai_agent import _one_shot

        self.assertEqual(_one_shot.run.__module__, "main")
        self.assertIn("dry_run", inspect.signature(_one_shot.run).parameters)

    def test_one_shot_consuma_la_coda_dei_topic(self):
        """`tube-assistant run` generava un topic a caso ignorando la coda."""
        import main

        with tempfile.TemporaryDirectory() as tmp:
            vecchio = os.getcwd()
            os.chdir(tmp)
            try:
                Path("state.json").write_text(
                    json.dumps({"topic_queue": ["primo", "secondo"]}), encoding="utf-8")
                self.assertEqual(main._topic_dalla_coda(), "primo")
                rimasti = json.loads(Path("state.json").read_text(encoding="utf-8"))
                self.assertEqual(rimasti["topic_queue"], ["secondo"])
                # coda vuota -> None, cosi' il chiamante genera con l'AI
                Path("state.json").write_text('{"topic_queue": []}', encoding="utf-8")
                self.assertIsNone(main._topic_dalla_coda())
            finally:
                os.chdir(vecchio)

    def test_workspace_configurato_a_mano_non_richiede_il_wizard(self):
        """Regressione: senza il marker `.setup_done` (che scrive solo il
        wizard) ogni comando rispediva al wizard, anche su un canale gia'
        funzionante — col rischio di sovrascrivere la configurazione."""
        from youtube_ai_agent.cli import _gia_configurato

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.assertFalse(_gia_configurato(ws))
            (ws / "credentials.json").write_text("{}", encoding="utf-8")
            (ws / "token.json").write_text("{}", encoding="utf-8")
            (ws / ".env").write_text(
                "PEXELS_API_KEY=k\nAI_SERVICE=ollama_cloud\nOLLAMA_API_KEY=x\n",
                encoding="utf-8")
            self.assertTrue(_gia_configurato(ws))
            # chiave AI mancante -> non configurato
            (ws / ".env").write_text("PEXELS_API_KEY=k\nAI_SERVICE=ollama_cloud\n",
                                     encoding="utf-8")
            self.assertFalse(_gia_configurato(ws))

    def test_cli_eseguibile_come_modulo(self):
        """Mancava `if __name__ == "__main__"`: `python -m youtube_ai_agent.cli`
        non faceva nulla e usciva 0, senza alcun messaggio."""
        sorgente = Path(__file__).parent.parent / "youtube_ai_agent" / "cli.py"
        self.assertIn('if __name__ == "__main__"',
                      sorgente.read_text(encoding="utf-8"))


class PackagingTests(unittest.TestCase):
    """Cio' che `uv tool install git+...` deve trovare nel pacchetto.

    Prima `assets/` e `.env.example` vivevano solo nella root del repo: il wheel
    non li conteneva, `_workspace.scaffold` non copiava nulla e chi installava
    dal Quick Start otteneva video senza musica e un `.env` di sette righe.
    """

    def test_template_env_e_bundled_nel_pacchetto(self):
        from youtube_ai_agent import _workspace

        template = _workspace._find_template(".env.example")
        self.assertIsNotNone(template, ".env.example non trovato ne' nel pacchetto ne' nella root")
        self.assertEqual(template.parent.name, "template")
        self.assertEqual(template.parent.parent.name, "youtube_ai_agent")
        contenuto = template.read_text(encoding="utf-8")
        for chiave in ("AI_SERVICE=", "PEXELS_API_KEY=", "TELEGRAM_BOT_TOKEN=", "VIDEO_SOURCE="):
            self.assertIn(chiave, contenuto)

    def test_musica_bundled_ha_tutti_i_mood(self):
        from youtube_ai_agent import _workspace
        from moduli.montaggio import ALLOWED_MOODS

        music = _workspace._package_assets() / "music"
        self.assertTrue(music.is_dir(), f"cartella musica assente: {music}")
        for mood in ALLOWED_MOODS:
            tracce = list((music / mood).glob("*.mp3"))
            self.assertTrue(tracce, f"nessuna traccia per il mood '{mood}'")

    def test_scaffold_copia_musica_e_template_nel_workspace(self):
        from youtube_ai_agent import _workspace

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            _workspace.scaffold(ws)
            env = (ws / ".env").read_text(encoding="utf-8")
            self.assertIn("VIDEO_SOURCE=", env, "scaffold ha scritto il .env minimale, non il template")
            self.assertTrue(list((ws / "assets" / "music").rglob("*.mp3")), "musica non copiata nel workspace")
            for d in ("output", "cache/pexels", "cache/ai_video", "logs", "assets/custom/clips"):
                self.assertTrue((ws / d).is_dir(), f"manca {d}")

    def test_pyproject_impacchetta_assets_e_template(self):
        import tomllib

        root = Path(__file__).resolve().parent.parent
        cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        st = cfg["tool"]["setuptools"]
        self.assertIn("youtube_ai_agent.assets", st["packages"])
        self.assertIn("youtube_ai_agent.template", st["packages"])
        self.assertEqual(st["package-dir"]["youtube_ai_agent.assets"], "assets")
        self.assertTrue(any(p.endswith("*.mp3") for p in st["package-data"]["youtube_ai_agent.assets"]))
        self.assertIn(".env.example", st["package-data"]["youtube_ai_agent.template"])
        self.assertEqual(cfg["project"]["scripts"], {"tube-assistant": "youtube_ai_agent.cli:main"})

    def test_script_di_avvio_usano_il_comando_giusto(self):
        root = Path(__file__).resolve().parent.parent
        for nome in ("start.sh", "install.sh", "installa.bat", "avvia_agente.bat"):
            testo = (root / nome).read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("youtube-ai-agent", testo, f"{nome} usa ancora il vecchio comando")
            self.assertIn("tube-assistant", testo, f"{nome} non richiama tube-assistant")

    def test_versione_pacchetto_allineata(self):
        import tomllib
        import youtube_ai_agent

        root = Path(__file__).resolve().parent.parent
        cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(youtube_ai_agent.__version__, cfg["project"]["version"])
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{cfg['project']['version']}]", changelog)
