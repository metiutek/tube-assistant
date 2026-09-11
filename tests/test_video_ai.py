"""Generazione delle clip con un'AI video al posto dello stock Pexels.

I test coprono le tre modalita' di `VIDEO_SOURCE`, il tetto di spesa, il
ripiego su Pexels quando il provider fallisce (un video senza b-roll e' peggio
di un video con b-roll stock) e il fatto che le clip pagate finiscano sui primi
segmenti invece che sparse a caso dallo shuffle.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moduli import asset, video_ai


class _EnvPulito(unittest.TestCase):
    """Ogni test parte da un ambiente noto: queste variabili vengono lette a
    ogni chiamata, quindi una lasciata sporca falsa i test successivi."""

    VARIABILI = ("VIDEO_SOURCE", "VIDEO_AI_PROVIDER", "VIDEO_AI_MODEL",
                 "VIDEO_AI_MAX_CLIPS", "REPLICATE_API_TOKEN", "FAL_KEY",
                 "LUMA_API_KEY", "OPENROUTER_API_KEY", "VIDEO_WIDTH",
                 "VIDEO_HEIGHT", "VIDEO_FPS")

    def setUp(self):
        self._salvate = {k: os.environ.get(k) for k in self.VARIABILI}
        for k in self.VARIABILI:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._salvate.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ConfigurazioneTests(_EnvPulito):
    def test_default_resta_pexels(self):
        self.assertEqual(video_ai.sorgente_video(), "pexels")
        pronto, motivo = video_ai.configurato()
        self.assertFalse(pronto)
        self.assertIn("pexels", motivo.lower())

    def test_valore_sconosciuto_non_rompe_la_pipeline(self):
        os.environ["VIDEO_SOURCE"] = "quantistico"
        os.environ["VIDEO_AI_PROVIDER"] = "inventato"
        self.assertEqual(video_ai.sorgente_video(), "pexels")
        self.assertEqual(video_ai.provider(), "replicate")

    def test_manca_la_chiave_lo_dice_prima_di_partire(self):
        os.environ["VIDEO_SOURCE"] = "ibrido"
        pronto, motivo = video_ai.configurato()
        self.assertFalse(pronto)
        self.assertIn("REPLICATE_API_TOKEN", motivo)

    def test_configurato_con_chiave(self):
        os.environ.update({"VIDEO_SOURCE": "ibrido", "REPLICATE_API_TOKEN": "r8_x"})
        pronto, motivo = video_ai.configurato()
        self.assertTrue(pronto)
        self.assertIn("Replicate", motivo)

    def test_ogni_provider_ha_chiave_e_modello_di_default(self):
        for pid, conf in video_ai.PROVIDER_DEFAULTS.items():
            self.assertTrue(conf["env_key"], pid)
            self.assertTrue(conf["modello"], pid)
            self.assertIn(pid, video_ai._GENERATORI)


class PromptTests(_EnvPulito):
    def test_la_keyword_di_ricerca_diventa_una_scena(self):
        """Una query per Pexels ("battery testing equipment") data cosi' com'e'
        a un modello video produce un fermo immagine: servono inquadratura,
        movimento e luce."""
        p = video_ai.costruisci_prompt("battery testing equipment", stile="documentary")
        self.assertTrue(p.startswith("battery testing equipment."))
        self.assertIn("documentary", p)
        self.assertIn("camera movement", p)
        self.assertIn("no text", p)


class TettoSpesaTests(_EnvPulito):
    def test_genera_pool_non_supera_mai_il_tetto(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "REPLICATE_API_TOKEN": "r8_x",
                           "VIDEO_AI_MAX_CLIPS": "3"})
        chiamate = []

        def finto(keyword, durata=5, stile=""):
            chiamate.append(keyword)
            return f"cache/ai_video/{keyword}.mp4"

        with patch.object(video_ai, "genera_clip", finto):
            prodotte = video_ai.genera_pool([f"kw{i}" for i in range(20)], 20,
                                            log=lambda _m: None)
        self.assertEqual(len(prodotte), 3)
        self.assertEqual(len(chiamate), 3)

    def test_una_clip_fallita_non_ferma_le_altre(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "REPLICATE_API_TOKEN": "r8_x",
                           "VIDEO_AI_MAX_CLIPS": "3"})

        def finto(keyword, durata=5, stile=""):
            if keyword == "kw1":
                raise video_ai.VideoAIError("credito esaurito")
            return f"cache/ai_video/{keyword}.mp4"

        with patch.object(video_ai, "genera_clip", finto):
            prodotte = video_ai.genera_pool(["kw0", "kw1", "kw2"], 3,
                                            log=lambda _m: None)
        self.assertEqual(prodotte, ["cache/ai_video/kw0.mp4", "cache/ai_video/kw2.mp4"])

    def test_ordine_delle_keyword_conservato(self):
        """Le keyword seguono il filo dello script: se il pool torna mescolato,
        mettere le clip AI in testa al montaggio non serve a niente."""
        os.environ.update({"VIDEO_SOURCE": "ai", "REPLICATE_API_TOKEN": "r8_x",
                           "VIDEO_AI_MAX_CLIPS": "5"})
        import time

        def finto(keyword, durata=5, stile=""):
            # la prima finisce per ultima: as_completed la restituirebbe in coda
            time.sleep(0.05 if keyword == "kw0" else 0)
            return f"cache/ai_video/{keyword}.mp4"

        with patch.object(video_ai, "genera_clip", finto):
            prodotte = video_ai.genera_pool(["kw0", "kw1", "kw2"], 3,
                                            log=lambda _m: None)
        self.assertEqual(prodotte, [f"cache/ai_video/kw{i}.mp4" for i in range(3)])


class PoolClipTests(_EnvPulito):
    KEYWORDS = [f"kw{i}" for i in range(6)]

    def _pexels_finto(self, keyword, max_n=3, extra_tags=None, escludi=None):
        esclusi = {os.path.abspath(p) for p in (escludi or ())}
        presi = []
        for j in range(60):
            path = f"{keyword}_p{j}"
            if os.path.abspath(path) in esclusi:
                continue
            presi.append(path)
            if len(presi) >= max_n:
                break
        return presi

    def _ai_finto(self, keywords, quante, durata=5, log=print):
        quante = min(quante, video_ai.max_clip(), len(keywords))
        return [f"cache/ai_video/ai_{i}.mp4" for i in range(quante)]

    def test_ibrido_mescola_ai_e_stock(self):
        os.environ.update({"VIDEO_SOURCE": "ibrido", "REPLICATE_API_TOKEN": "r8_x",
                           "VIDEO_AI_MAX_CLIPS": "4"})
        with patch.object(asset, "scarica_clips", self._pexels_finto), \
             patch.object(video_ai, "genera_pool", self._ai_finto):
            pool = asset.scarica_pool_clips(self.KEYWORDS, None, log=lambda _m: None)
        ai = [k for k in pool if k.startswith("ai#")]
        self.assertEqual(len(ai), 4)
        self.assertGreater(len(pool), len(ai), "lo stock deve completare il pool")
        self.assertEqual(len(set(pool.values())), len(pool))

    def test_solo_ai_non_scarica_da_pexels(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "REPLICATE_API_TOKEN": "r8_x",
                           "VIDEO_AI_MAX_CLIPS": "4"})

        def mai(*a, **kw):
            raise AssertionError("in modalita' 'ai' Pexels non va chiamato")

        with patch.object(asset, "scarica_clips", mai), \
             patch.object(video_ai, "genera_pool", self._ai_finto):
            pool = asset.scarica_pool_clips(self.KEYWORDS, None, log=lambda _m: None)
        self.assertEqual(len(pool), 4)
        self.assertTrue(all(k.startswith("ai#") for k in pool))

    def test_provider_giu_ripiega_su_pexels(self):
        """Un video con b-roll stock e' molto meglio di nessun video."""
        os.environ.update({"VIDEO_SOURCE": "ai", "REPLICATE_API_TOKEN": "r8_x"})

        def esplode(*a, **kw):
            raise RuntimeError("provider irraggiungibile")

        with patch.object(asset, "scarica_clips", self._pexels_finto), \
             patch.object(video_ai, "genera_pool", esplode):
            pool = asset.scarica_pool_clips(self.KEYWORDS, None, log=lambda _m: None)
        self.assertTrue(pool)
        self.assertFalse(any(k.startswith("ai#") for k in pool))


class OrdineMontaggioTests(unittest.TestCase):
    def test_le_clip_ai_aprono_il_video(self):
        """Generarle e poi lasciarle allo shuffle vuol dire pagarle per vederle
        comparire al minuto sette, quando chi doveva andarsene e' gia' andato."""
        from moduli.montaggio import _build_clip_sequence, _clip_prioritarie

        clip_paths = {"ai#0": "A1", "ai#1": "A2",
                      "kw#2": "P1", "kw#3": "P2", "kw#4": "P3"}
        keywords = list(clip_paths)
        files = list(clip_paths.values())
        prio = _clip_prioritarie(keywords, clip_paths, files)
        self.assertEqual(prio, ["A1", "A2"])
        seq = _build_clip_sequence(files, 8, prioritarie=prio)
        self.assertEqual(seq[:2], ["A1", "A2"])
        self.assertEqual(len(seq), 8)

    def test_senza_clip_ai_il_comportamento_non_cambia(self):
        from moduli.montaggio import _build_clip_sequence

        seq = _build_clip_sequence(["P1", "P2", "P3"], 3)
        self.assertEqual(sorted(seq), ["P1", "P2", "P3"])

    def test_prioritaria_non_leggibile_viene_ignorata(self):
        """`_filtra_clip_leggibili` puo' aver scartato una clip corrotta: non
        deve finire lo stesso nella sequenza."""
        from moduli.montaggio import _build_clip_sequence

        seq = _build_clip_sequence(["P1", "P2"], 4, prioritarie=["ROTTA", "P2"])
        self.assertEqual(seq[0], "P2")
        self.assertNotIn("ROTTA", seq)


class CacheAITests(unittest.TestCase):
    def test_cache_ai_ha_un_tetto_separato(self):
        """Le clip generate sono costate soldi: non devono essere sfrattate da
        uno stock gratuito appena scaricato."""
        from moduli import manutenzione

        with tempfile.TemporaryDirectory() as tmp:
            pexels_dir = os.path.join(tmp, "pexels")
            ai_dir = os.path.join(tmp, "ai")
            os.makedirs(pexels_dir)
            os.makedirs(ai_dir)
            for i in range(4):
                Path(pexels_dir, f"p{i}.mp4").write_bytes(b"x" * 400_000)
            costosa = Path(ai_dir, "ai0.mp4")
            costosa.write_bytes(b"x" * 400_000)
            os.utime(costosa, (1_000_000, 1_000_000))  # la piu' vecchia di tutte

            with patch.object(manutenzione, "CACHE_DIR", pexels_dir), \
                 patch.object(manutenzione, "AI_CACHE_DIR", ai_dir), \
                 patch.object(manutenzione, "MAX_CACHE_MB", 1.0), \
                 patch.object(manutenzione, "MAX_AI_CACHE_MB", 100.0):
                manutenzione.pulisci_cache()

            self.assertTrue(costosa.exists(),
                            "la clip AI e' stata cancellata dal tetto di Pexels")
            rimaste = list(Path(pexels_dir).glob("*.mp4"))
            self.assertLess(len(rimaste), 4, "il tetto Pexels non ha potato nulla")


class ImpostazioniTests(unittest.TestCase):
    def test_stato_leggibile_da_env(self):
        from moduli import impostazioni_video

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text(
                "VIDEO_SOURCE=ibrido\nVIDEO_AI_PROVIDER=luma\n"
                "LUMA_API_KEY=abc\nVIDEO_AI_MAX_CLIPS=9\n", encoding="utf-8")
            riga = impostazioni_video.stato(tmp)
        self.assertIn("ibrido", riga)
        self.assertIn("Luma", riga)
        self.assertIn("9", riga)
        self.assertNotIn("abc", riga, "la chiave API non va mai stampata")

    def test_scrivi_env_aggiorna_anche_il_processo(self):
        """Il test di generazione parte subito dopo il salvataggio: se leggesse
        ancora la configurazione vecchia direbbe 'ok' sulla chiave sbagliata."""
        from moduli import impostazioni_video

        with tempfile.TemporaryDirectory() as tmp:
            precedente = os.environ.get("VIDEO_AI_MAX_CLIPS")
            try:
                impostazioni_video.scrivi_env(tmp, "VIDEO_AI_MAX_CLIPS", "7")
                self.assertEqual(os.environ["VIDEO_AI_MAX_CLIPS"], "7")
                self.assertIn("VIDEO_AI_MAX_CLIPS=7",
                              Path(tmp, ".env").read_text(encoding="utf-8"))
            finally:
                if precedente is None:
                    os.environ.pop("VIDEO_AI_MAX_CLIPS", None)
                else:
                    os.environ["VIDEO_AI_MAX_CLIPS"] = precedente

    def test_la_tui_espone_tutte_le_modalita(self):
        from moduli import impostazioni_video

        ids = {s["id"] for s in impostazioni_video.SORGENTI.values()}
        self.assertEqual(ids, set(video_ai.SORGENTI))

    def test_comando_clips_registrato_nella_cli(self):
        from youtube_ai_agent import cli

        self.assertIn("clips", cli._COMMANDS)
        self.assertIn("clips", [cmd for _label, cmd in cli._MENU_ITEMS])


class ProviderHTTPTests(_EnvPulito):
    """Le risposte dei tre provider hanno forme diverse: qui si verifica che
    ognuna venga letta correttamente, senza rete."""

    class _Risposta:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(response=self)

    def test_replicate_legge_output_lista(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "VIDEO_AI_PROVIDER": "replicate",
                           "REPLICATE_API_TOKEN": "r8_x"})
        completata = self._Risposta({"status": "succeeded",
                                     "output": ["https://cdn/clip.mp4"]})
        with patch.object(video_ai.requests, "post", return_value=completata):
            self.assertEqual(video_ai._genera_replicate("p", 5), "https://cdn/clip.mp4")

    def test_replicate_fallita_solleva_errore_gestito(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "VIDEO_AI_PROVIDER": "replicate",
                           "REPLICATE_API_TOKEN": "r8_x"})
        fallita = self._Risposta({"status": "failed", "error": "NSFW"})
        with patch.object(video_ai.requests, "post", return_value=fallita):
            with self.assertRaises(video_ai.VideoAIError):
                video_ai._genera_replicate("p", 5)

    def test_fal_segue_la_coda(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "VIDEO_AI_PROVIDER": "fal",
                           "FAL_KEY": "k"})
        accodata = self._Risposta({"status_url": "https://s", "response_url": "https://r"})
        risposte = [self._Risposta({"status": "COMPLETED"}),
                    self._Risposta({"video": {"url": "https://cdn/f.mp4"}})]
        with patch.object(video_ai.requests, "post", return_value=accodata), \
             patch.object(video_ai.requests, "get", side_effect=risposte), \
             patch.object(video_ai.time, "sleep", lambda _s: None):
            self.assertEqual(video_ai._genera_fal("p", 5), "https://cdn/f.mp4")

    def test_luma_attende_lo_stato_completed(self):
        os.environ.update({"VIDEO_SOURCE": "ai", "VIDEO_AI_PROVIDER": "luma",
                           "LUMA_API_KEY": "k"})
        creata = self._Risposta({"id": "gen1"})
        stati = [self._Risposta({"state": "dreaming"}),
                 self._Risposta({"state": "completed",
                                 "assets": {"video": "https://cdn/l.mp4"}})]
        with patch.object(video_ai.requests, "post", return_value=creata), \
             patch.object(video_ai.requests, "get", side_effect=stati), \
             patch.object(video_ai.time, "sleep", lambda _s: None):
            self.assertEqual(video_ai._genera_luma("p", 5), "https://cdn/l.mp4")

    def test_chiave_rifiutata_diventa_messaggio_leggibile(self):
        import requests

        os.environ.update({"VIDEO_SOURCE": "ai", "VIDEO_AI_PROVIDER": "replicate",
                           "REPLICATE_API_TOKEN": "sbagliata"})
        risposta = self._Risposta({"detail": "Unauthenticated"}, status=401)

        def _solleva(*a, **kw):
            raise requests.HTTPError(response=risposta)

        with patch.object(video_ai, "_GENERATORI", {"replicate": _solleva}):
            with self.assertRaises(video_ai.VideoAIError) as ctx:
                video_ai.genera_clip("qualcosa")
        self.assertIn("REPLICATE_API_TOKEN", str(ctx.exception))


def _ha_ffmpeg() -> bool:
    from moduli.ffmpeg_utils import ffmpeg_path
    try:
        ffmpeg_path()
        return True
    except Exception:
        return False


class OpenRouterTests(_EnvPulito):
    """OpenRouter non ha modelli text-to-video: da' un'immagine, e la clip la
    monta ffmpeg. Qui si verifica la lettura della risposta e il fatto che un
    generatore che ritorna un PATH (invece di un URL) finisca comunque in cache.
    """

    def setUp(self):
        super().setUp()
        os.environ.update({"VIDEO_SOURCE": "ibrido",
                           "VIDEO_AI_PROVIDER": "openrouter",
                           "OPENROUTER_API_KEY": "sk-or-x"})

    def test_configurato_con_la_chiave_degli_script(self):
        pronto, motivo = video_ai.configurato()
        self.assertTrue(pronto, motivo)
        self.assertIn("gemini-2.5-flash-image", motivo)

    def test_prompt_per_immagine_non_chiede_movimento_di_camera(self):
        """Chiedere "slow camera movement" a un modello di immagini spreca
        prompt: il movimento lo aggiunge ffmpeg dopo."""
        p = video_ai.costruisci_prompt("battery testing equipment", stile="documentary")
        self.assertTrue(p.startswith("battery testing equipment."))
        self.assertIn("film still", p)
        self.assertNotIn("camera movement", p)
        self.assertIn("no text", p)

    def test_legge_immagine_da_data_uri(self):
        import base64

        payload = {"choices": [{"message": {"images": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," +
                           base64.b64encode(b"PNGDATA").decode()}}]}}]}
        with patch.object(video_ai.requests, "post",
                          return_value=ProviderHTTPTests._Risposta(payload)):
            self.assertEqual(video_ai._immagine_openrouter("p"), b"PNGDATA")

    def test_immagine_su_url_viene_scaricata(self):
        payload = {"choices": [{"message": {"images": [
            {"image_url": {"url": "https://cdn/scena.png"}}]}}]}

        class _Binaria:
            content = b"JPEGDATA"

            def raise_for_status(self):
                pass

        with patch.object(video_ai.requests, "post",
                          return_value=ProviderHTTPTests._Risposta(payload)), \
             patch.object(video_ai.requests, "get", return_value=_Binaria()):
            self.assertEqual(video_ai._immagine_openrouter("p"), b"JPEGDATA")

    def test_modello_solo_testo_lo_dice_invece_di_sembrare_chiave_sbagliata(self):
        os.environ["VIDEO_AI_MODEL"] = "meta-llama/llama-3.3-70b-instruct"
        payload = {"choices": [{"message": {"content": "certo, ecco la scena..."}}]}
        with patch.object(video_ai.requests, "post",
                          return_value=ProviderHTTPTests._Risposta(payload)):
            with self.assertRaises(video_ai.VideoAIError) as ctx:
                video_ai._immagine_openrouter("p")
        self.assertIn("output image", str(ctx.exception))

    def test_clip_locale_finisce_nella_cache_come_quelle_scaricate(self):
        """Il generatore a immagini ritorna un path, non un URL: `genera_clip`
        deve accettarlo senza passare dal download."""
        with tempfile.TemporaryDirectory() as tmp:
            prodotta = Path(tmp) / "generata.mp4"
            prodotta.write_bytes(b"0" * 20_000)
            dest = video_ai._percorso_cache(
                video_ai.costruisci_prompt("scena", stile="cinematic"))
            with patch.object(video_ai, "_GENERATORI",
                              {"openrouter": lambda prompt, durata: str(prodotta)}), \
                 patch.object(video_ai, "_percorso_cache",
                              lambda _p: str(Path(tmp) / "cache.mp4")), \
                 patch.object(video_ai, "_scarica",
                              side_effect=AssertionError("non deve scaricare nulla")):
                percorso = video_ai.genera_clip("scena")
            self.assertEqual(percorso, str(Path(tmp) / "cache.mp4"))
            self.assertTrue(os.path.exists(percorso))
            self.assertFalse(prodotta.exists(), "il file va spostato, non copiato")
            self.assertTrue(dest)

    def test_clip_troncata_non_entra_in_cache(self):
        """Un'immagine mezza scritta diventerebbe una clip nera nel montaggio."""
        with tempfile.TemporaryDirectory() as tmp:
            monca = Path(tmp) / "monca.mp4"
            monca.write_bytes(b"0" * 100)
            with patch.object(video_ai, "_GENERATORI",
                              {"openrouter": lambda prompt, durata: str(monca)}), \
                 patch.object(video_ai, "_percorso_cache",
                              lambda _p: str(Path(tmp) / "cache.mp4")):
                with self.assertRaises(video_ai.VideoAIError):
                    video_ai.genera_clip("scena")

    def test_movimenti_di_camera_diversi_tra_clip(self):
        """Quattro clip che zoomano tutte allo stesso modo si notano."""
        mosse = {video_ai._movimento(f"scena {i}", 150) for i in range(12)}
        self.assertGreater(len(mosse), 1)
        # deterministico: la clip ripescata dalla cache si muove come prima
        self.assertEqual(video_ai._movimento("scena 1", 150),
                         video_ai._movimento("scena 1", 150))


@unittest.skipUnless(_ha_ffmpeg(), "richiede ffmpeg")
class KenBurnsTests(_EnvPulito):
    """L'animazione dell'immagine e' l'unico pezzo di questo provider che puo'
    fallire in silenzio: ffmpeg esce 0 e produce un file inutilizzabile."""

    def _png(self, percorso: Path) -> bytes:
        from PIL import Image
        Image.new("RGB", (1024, 768), (30, 60, 120)).save(percorso)
        return percorso.read_bytes()

    def test_immagine_diventa_clip_della_durata_e_risoluzione_giuste(self):
        import subprocess

        from moduli.ffmpeg_utils import ffprobe_path

        os.environ.update({"VIDEO_WIDTH": "1280", "VIDEO_HEIGHT": "720",
                           "VIDEO_FPS": "24"})
        with tempfile.TemporaryDirectory() as tmp:
            dati = self._png(Path(tmp) / "scena.png")
            dest = str(Path(tmp) / "clip.mp4")
            video_ai._clip_da_immagine(dati, 3, "prompt di prova", dest)
            self.assertTrue(os.path.exists(dest))
            sonda = subprocess.run(
                [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", dest],
                capture_output=True, text=True)
            valori = sonda.stdout.split()
            self.assertEqual(valori[0], "1280")
            self.assertEqual(valori[1], "720")
            self.assertAlmostEqual(float(valori[2]), 3.0, delta=0.4)
            # nessun file di lavoro lasciato indietro
            self.assertEqual(sorted(os.listdir(tmp)), ["clip.mp4", "scena.png"])

    def test_la_clip_si_muove_davvero(self):
        """Senza zoompan ffmpeg produce comunque un mp4 valido, ma fermo: due
        fotogrammi distanti devono essere diversi."""
        import subprocess

        from moduli.ffmpeg_utils import ffmpeg_path

        os.environ.update({"VIDEO_WIDTH": "640", "VIDEO_HEIGHT": "360",
                           "VIDEO_FPS": "24"})
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image
            sorgente = Path(tmp) / "scena.png"
            img = Image.new("RGB", (1024, 768), (20, 20, 20))
            for x in range(0, 1024, 8):          # trama fine: uno zoom la sposta
                for y in range(768):
                    img.putpixel((x, y), (240, 240, 240))
            img.save(sorgente)
            dest = str(Path(tmp) / "clip.mp4")
            video_ai._clip_da_immagine(sorgente.read_bytes(), 3, "prova", dest)
            for istante, nome in ((0.2, "a.png"), (2.5, "b.png")):
                subprocess.run([ffmpeg_path(), "-y", "-ss", str(istante), "-i", dest,
                                "-frames:v", "1", str(Path(tmp) / nome)],
                               capture_output=True)
            a = (Path(tmp) / "a.png").read_bytes()
            b = (Path(tmp) / "b.png").read_bytes()
            self.assertTrue(a and b)
            self.assertNotEqual(a, b, "la clip e' un fermo immagine")


if __name__ == "__main__":
    unittest.main()
