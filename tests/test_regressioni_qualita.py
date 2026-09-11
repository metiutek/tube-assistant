"""Regressioni sulla QUALITA' del video prodotto.

Ogni test qui copre un difetto trovato ispezionando un video vero uscito dalla
pipeline: audio consegnato piano e mono, pool di clip sotto il numero di
segmenti, clip fuori tema, script piu' corto della durata chiesta, sottotitoli
assenti nella modalita' one-shot, barra di avanzamento ferma all'80%,
copertina con il testo a ridosso del bordo inferiore.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _ha_ffmpeg() -> bool:
    from moduli.ffmpeg_utils import ffmpeg_path
    try:
        ffmpeg_path()
        return True
    except FileNotFoundError:
        return False


class AudioConsegnaTests(unittest.TestCase):
    """L'audio finale usciva com'era uscito dal TTS — mono 24 kHz, ~72 kb/s,
    media -26 dB — cioe' molto piu' piano di qualunque altro video su YouTube,
    che consegna intorno a -14 LUFS."""

    def test_mux_normalizza_e_porta_a_stereo(self):
        from moduli import montaggio

        catturati = []

        class _Esito:
            returncode = 0
            stderr = ""

        def _fake_run(cmd, **kwargs):
            catturati.append(cmd)
            return _Esito()

        with patch.object(montaggio.subprocess, "run", _fake_run):
            montaggio._mux_audio_ffmpeg("v.mp4", "a.mp3", "out.mp4", None, 10.0)
            montaggio._mux_audio_ffmpeg("v.mp4", "a.mp3", "out.mp4", "bg.mp3", 10.0)

        self.assertEqual(len(catturati), 2)
        for cmd in catturati:
            self.assertIn("loudnorm", " ".join(cmd))
            self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")
            self.assertEqual(cmd[cmd.index("-ac") + 1], "2")

    def test_narrazione_normalizzata_dopo_il_tts(self):
        """Il percorso moviepy non passa dal mux: la narrazione deve arrivargli
        gia' al volume giusto."""
        from moduli import audio

        with patch.object(audio, "_edge_tts", return_value=True), \
             patch.object(audio, "_audio_valido", return_value=True), \
             patch.object(audio, "_normalizza") as norm:
            audio.genera_audio("testo di prova", "out.mp3")
        norm.assert_called_once_with("out.mp3")


class PoolClipTests(unittest.TestCase):
    """Con 18 keyword servivano 96 clip distinte, ma il tetto di 5 per keyword
    ne rendeva al massimo 90 e le keyword povere lo abbassavano a 80: una
    ventina di segmenti riusavano lo stesso materiale."""

    def _pool(self, disponibili, audio_path):
        from moduli import asset

        def finto(keyword, max_n=3, extra_tags=None, escludi=None):
            esclusi = {os.path.abspath(p) for p in (escludi or ())}
            presi = []
            for path in disponibili[keyword]:
                if os.path.abspath(path) in esclusi:
                    continue
                presi.append(path)
                if len(presi) >= max_n:
                    break
            return presi

        with patch.object(asset, "scarica_clips", finto):
            return asset.scarica_pool_clips(list(disponibili), audio_path,
                                            log=lambda _msg: None)

    @unittest.skipUnless(_ha_ffmpeg(), "serve ffprobe per leggere la durata")
    def test_keyword_povere_compensate_dalle_altre(self):
        from moduli.ffmpeg_utils import ffmpeg_path

        # 5 keyword rendono solo 3 clip, le altre 13 ne hanno in abbondanza
        disponibili = {f"kw{i}": [f"kw{i}_c{j}" for j in range(3 if i < 5 else 40)]
                       for i in range(18)}
        with tempfile.TemporaryDirectory() as tmp:
            voce = os.path.join(tmp, "voce.wav")
            subprocess.run(
                [ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi",
                 "-i", "anullsrc=r=44100:cl=mono", "-t", "480", voce],
                check=True, capture_output=True,
            )
            pool = self._pool(disponibili, voce)
        # 480s a 5s per segmento = 97 clip: prima ci si fermava a 80
        self.assertGreaterEqual(len(set(pool.values())), 96)
        self.assertEqual(len(pool), len(set(pool.values())),
                         "il pool contiene due volte la stessa clip")


class RilevanzaClipTests(unittest.TestCase):
    def test_clip_che_nomina_il_soggetto_viene_prima(self):
        """"laboratory battery testing equipment" restituiva come primo
        risultato Pexels una centrifuga per provette di sangue."""
        from moduli.asset import _ordina_per_rilevanza

        video = [
            {"id": 1, "url": "https://www.pexels.com/video/laboratory-centrifuge-spinning-blood-test-tubes-1/"},
            {"id": 2, "url": "https://www.pexels.com/video/lithium-battery-cells-on-a-testing-rig-2/"},
            {"id": 3, "url": "https://www.pexels.com/video/metal-cups-on-a-table-3/"},
        ]
        ordine = [v["id"] for v in
                  _ordina_per_rilevanza(video, "laboratory battery testing equipment")]
        self.assertEqual(ordine[0], 2)
        self.assertEqual(ordine[-1], 3)

    def test_senza_url_resta_l_ordine_di_pexels(self):
        from moduli.asset import _ordina_per_rilevanza

        video = [{"id": 1}, {"id": 2}]
        self.assertEqual([v["id"] for v in _ordina_per_rilevanza(video, "qualcosa")],
                         [1, 2])


class CacheLRUTests(unittest.TestCase):
    def test_clip_riusata_non_e_piu_la_piu_vecchia(self):
        """`pulisci_cache` pota per mtime, cioe' per data di DOWNLOAD: una clip
        ripescata dalla cache a ogni video veniva cancellata prima di una
        scaricata una volta sola e mai piu' usata."""
        from moduli.asset import _tocca

        with tempfile.TemporaryDirectory() as tmp:
            vecchia = os.path.join(tmp, "vecchia.mp4")
            nuova = os.path.join(tmp, "nuova.mp4")
            Path(vecchia).write_bytes(b"x")
            Path(nuova).write_bytes(b"x")
            os.utime(vecchia, (1_000_000, 1_000_000))
            os.utime(nuova, (2_000_000, 2_000_000))
            _tocca(vecchia)
            self.assertGreater(os.path.getmtime(vecchia), os.path.getmtime(nuova))


class SottotitoliOneShotTests(unittest.TestCase):
    def test_run_one_shot_carica_i_sottotitoli(self):
        """`agent.py` generava e caricava l'SRT, `main.py` (usato da
        `tube-assistant run`) no: stesso contenuto pubblicato con o senza
        sottotitoli a seconda di come era partita la pipeline."""
        import inspect
        import main

        self.assertIn("_carica_sottotitoli", inspect.getsource(main.run))
        sorgente = inspect.getsource(main._carica_sottotitoli)
        self.assertIn("genera_srt", sorgente)
        self.assertIn("carica_sottotitoli", sorgente)


class ProgressoRenderTests(unittest.TestCase):
    def test_le_fasi_finali_riportano_avanzamento(self):
        """La barra si fermava a "80% - ETA 0m 0s" per tutta la durata di
        concat e mux: sembrava un render piantato."""
        import inspect
        from moduli import montaggio

        sorgente = inspect.getsource(montaggio._monta_video_ffmpeg)
        for tappa in ("on_progress(80,", "on_progress(90,", "on_progress(100,"):
            self.assertIn(tappa, sorgente)


class ScriptCortoTests(unittest.TestCase):
    def test_script_corto_viene_continuato_non_rigenerato(self):
        """Rigenerare l'intero JSON produceva ogni volta la stessa lunghezza
        (742 -> 646 -> 696 su target 1200) bruciando tre chiamate."""
        from moduli import cervello

        with patch.object(cervello, "chat_ollama",
                          return_value="parola " * 500) as mock:
            esteso = cervello._allunga_script(
                {"script": "parola " * 700}, "topic", 1200, 960, "italian", 8)
        self.assertEqual(mock.call_count, 1)
        self.assertGreaterEqual(len(esteso["script"].split()), 960)

    def test_continuazione_in_json_viene_scartata(self):
        """Il TTS leggerebbe ad alta voce graffe e nomi dei campi."""
        from moduli import cervello

        with patch.object(cervello, "chat_ollama",
                          return_value='{"script": "' + "parola " * 500 + '"}'):
            esteso = cervello._allunga_script(
                {"script": "parola " * 700}, "topic", 1200, 960, "italian", 8)
        self.assertEqual(len(esteso["script"].split()), 700)


class MargineThumbnailTests(unittest.TestCase):
    def test_il_testo_resta_sopra_la_zona_della_durata(self):
        """YouTube stampa la durata del video nell'angolo in basso a destra
        della copertina: il testo non deve finirci sotto."""
        from PIL import Image
        from moduli.thumbnail import (
            _draw_title, MARGINE_SICURO_BASSO, THUMB_W, THUMB_H,
        )

        for testo, preset in (("BATTERY BREAKTHROUGH?", "A"),
                              ("Why AI judges gpq everything", "C"),
                              ("QUANTUM", "A")):
            img = _draw_title(Image.new("RGB", (THUMB_W, THUMB_H), (0, 0, 0)),
                              testo, font_size_preset=preset).convert("L")
            px = img.load()
            righe = [y for y in range(THUMB_H)
                     if sum(1 for x in range(0, THUMB_W, 2) if px[x, y] > 200) > 2]
            self.assertTrue(righe, f"nessun testo disegnato per {testo!r}")
            self.assertGreaterEqual(THUMB_H - 1 - righe[-1], MARGINE_SICURO_BASSO,
                                    f"testo troppo in basso per {testo!r}")

    def test_immagine_sotto_misura_viene_portata_a_1280x720(self):
        """Pollinations ignora width/height e consegna 1024x576: la copertina
        va comunque caricata a 1280x720."""
        from PIL import Image
        from moduli.thumbnail import _porta_a_misura, THUMB_W, THUMB_H

        piccola = Image.new("RGB", (1024, 576), (30, 30, 30))
        self.assertEqual(_porta_a_misura(piccola).size, (THUMB_W, THUMB_H))


class LockAvvioTests(unittest.TestCase):
    def test_lock_esclusivo_vede_il_file_gia_bloccato(self):
        """Il PID da solo non bastava: dopo un kill duro il file resta orfano e
        un PID riciclato faceva credere che l'agente fosse ancora vivo."""
        import agent

        with tempfile.TemporaryDirectory() as tmp:
            percorso = os.path.join(tmp, "prova.pid")
            with open(percorso, "w+") as primo:
                self.assertTrue(agent._prova_lock_esclusivo(primo))
                with open(percorso, "r+") as secondo:
                    self.assertFalse(agent._prova_lock_esclusivo(secondo))


if __name__ == "__main__":
    unittest.main()
