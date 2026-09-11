"""Regressioni sui moduli che prima non avevano copertura.

I bug corretti qui (video piu' corto dell'audio, caption invisibile, orari
incoerenti, preferenze sovrascritte da una domanda, limiti API YouTube) sono
tutti sfuggiti perche' montaggio/thumbnail/asset/audio/preferenze non erano
testati: le prove stavano solo su state, telegram e ai_client.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _ha_ffmpeg() -> bool:
    from moduli.ffmpeg_utils import ffmpeg_path
    try:
        ffmpeg_path()
        return True
    except FileNotFoundError:
        return False


class SchedulingTests(unittest.TestCase):
    def test_trigger_e_publish_restano_coerenti(self):
        """Regressione: con auto_scheduling attivo il daemon produceva su
        best_hours_utc mentre /orari e la pubblicazione usavano
        publish_hours_utc — tre orari diversi per lo stesso video."""
        from moduli.scheduling import trigger_hours, publish_hours, TRIGGER_LEAD_HOURS

        stato = {
            "videos_per_day": 2,
            "auto_scheduling": True,
            "best_hours_utc": [9, 21],
            "publish_hours_utc": [12, 20],
        }
        pubblica = publish_hours(stato)
        produce = trigger_hours(stato)
        self.assertEqual(pubblica, [9, 21])
        self.assertEqual(produce, sorted((h - TRIGGER_LEAD_HOURS) % 24 for h in pubblica))

    def test_piu_video_al_giorno_hanno_piu_trigger(self):
        """Regressione: con videos_per_day=3 e nessun orario configurato il
        default dava un solo trigger, quindi 2 video su 3 non partivano mai."""
        from moduli.scheduling import trigger_hours, publish_hours

        self.assertEqual(len(trigger_hours({"videos_per_day": 3})), 3)
        self.assertEqual(len(publish_hours({"videos_per_day": 3})), 3)

    def test_ore_non_valide_in_state_sono_ignorate(self):
        from moduli.scheduling import publish_hours

        self.assertEqual(publish_hours({"publish_hours_utc": ["boh", 99, -3]}), [20])

    def test_publish_slot_futuro(self):
        from datetime import datetime, timezone
        from moduli.scheduling import prossima_pubblicazione

        adesso = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
        slot = prossima_pubblicazione({"publish_hours_utc": [20]}, 0, now=adesso)
        self.assertGreater(slot, adesso)
        self.assertEqual(slot.hour, 20)


class PreferenzeTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_domande_non_scrivono_preferenze(self):
        """Regressione: "che lingua parla il canale?" salvava
        lingua="parla il canale?" e il messaggio non arrivava all'AI."""
        from moduli.preferenze import aggiorna_da_testo

        for domanda in (
            "che lingua parla il canale?",
            "di cosa parla il video?",
            "quanto dura il prossimo video?",
            "mi dici il ritmo attuale",
        ):
            self.assertEqual(aggiorna_da_testo(domanda), {}, domanda)

    def test_generazione_oneshot_non_scrive_stile(self):
        """Regressione: "genera una copertina di prova" salvava
        stile_thumbnail="di prova"."""
        from moduli.preferenze import aggiorna_da_testo

        self.assertEqual(aggiorna_da_testo("genera una copertina di prova"), {})

    def test_comandi_veri_funzionano_ancora(self):
        from moduli.preferenze import aggiorna_da_testo

        self.assertEqual(aggiorna_da_testo("metti la lingua italiana")["lingua"], "italian")
        self.assertEqual(aggiorna_da_testo("lingua: english")["lingua"], "english")
        self.assertEqual(aggiorna_da_testo("voglio video da 5 minuti")["durata_target_minuti"], 5)
        self.assertEqual(aggiorna_da_testo("stile thumbnail dark neon")["stile_thumbnail"], "dark neon")


class PubblicaLimitiTests(unittest.TestCase):
    def test_metadati_entro_i_limiti_api(self):
        """Regressione: titolo/descrizione/tag oltre i limiti YouTube facevano
        fallire l'insert con HTTP 400 a video gia' renderizzato."""
        from moduli.pubblica import (
            _snippet_valido, MAX_TITLE_CHARS, MAX_DESCRIPTION_CHARS, MAX_TAG_CHARS,
        )

        snippet = _snippet_valido({
            "title": "T" * 300,
            "description": "parola " * 2000,
            "tags": [f"tag numero {i}" for i in range(300)],
        })
        self.assertLessEqual(len(snippet["title"]), MAX_TITLE_CHARS)
        self.assertLessEqual(len(snippet["description"]), MAX_DESCRIPTION_CHARS)
        costo = sum(len(t) + (2 if " " in t else 0) + 1 for t in snippet["tags"])
        self.assertLessEqual(costo, MAX_TAG_CHARS)

    def test_titolo_senza_angolari(self):
        from moduli.pubblica import _snippet_valido

        self.assertNotIn("<", _snippet_valido({"title": "AI <b>hack</b>", "tags": []})["title"])

    def test_titolo_vuoto_e_errore_esplicito(self):
        from moduli.pubblica import _snippet_valido

        with self.assertRaises(ValueError):
            _snippet_valido({"title": "   ", "tags": []})


class CaptionTests(unittest.TestCase):
    def test_percentuale_non_viene_escapata(self):
        """Regressione: "\\%" faceva "Stray %" e drawtext usciva con codice 0
        SENZA disegnare nulla — la caption spariva in silenzio."""
        from moduli.montaggio import _caption_filter

        vf = _caption_filter("50% FASTER")
        self.assertIn("expansion=none", vf)
        self.assertNotIn("\\%", vf)

    def test_apostrofi_sostituiti(self):
        """Un apice dentro text='...' non e' escapabile in ffmpeg."""
        from moduli.montaggio import _caption_text

        testo = _caption_text("Don't stop")
        self.assertNotIn("'", testo)
        self.assertIn("DON", testo)

    def test_caption_entra_nella_larghezza(self):
        """Regressione: limite fisso di 78 caratteri, tagliato ai bordi appena
        il video non era 1920px (drawtext non va a capo)."""
        from moduli.montaggio import _caption_text, _max_caption_chars

        lunga = "UNA FRASE DAVVERO MOLTO LUNGA CHE NON STAREBBE MAI SU UNA RIGA SOLA DI SCHERMO"
        self.assertLessEqual(len(_caption_text(lunga)), _max_caption_chars())

    def test_niente_punteggiatura_penzolante(self):
        from moduli.montaggio import _caption_text

        self.assertFalse(_caption_text("Uno, due, tre, quattro").endswith(","))


class ClipCacheTests(unittest.TestCase):
    def test_una_parola_in_comune_non_basta(self):
        """Regressione: "AI neural network" pescava dalla cache una clip
        taggata "AI robot" e il video si riempiva di footage scollegato."""
        from moduli.asset import _match_tag

        self.assertFalse(_match_tag({"ai neural network", "ai", "neural", "network"},
                                    {"ai robot", "ai", "robot"}))

    def test_keyword_intera_o_due_parole_matchano(self):
        from moduli.asset import _match_tag

        self.assertTrue(_match_tag({"ai neural network"}, {"ai neural network", "ai"}))
        self.assertTrue(_match_tag({"neural", "network", "ai"},
                                   {"neural", "network", "brain"}))


class AudioTests(unittest.TestCase):
    def test_frase_lunghissima_viene_spezzata(self):
        """Regressione: una frase senza punteggiatura piu' lunga di CHUNK_WORDS
        finiva in un chunk unico che sforava il timeout TTS."""
        from moduli.audio import _split_chunks, CHUNK_WORDS

        chunks = _split_chunks("parola " * 900)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.split()), CHUNK_WORDS)

    def test_testo_vuoto(self):
        from moduli.audio import _split_chunks

        self.assertEqual(_split_chunks(""), [])

    def test_concat_senza_ffmpeg_solleva_errore(self):
        """Regressione: il fallback byte-concat produceva un MP3 la cui durata
        dichiarata era quella del primo chunk, e il montaggio ci costruiva
        sopra un video lungo una frazione dell'audio."""
        from unittest.mock import patch
        from moduli import audio

        with tempfile.TemporaryDirectory() as tmp:
            parti = []
            for i in range(2):
                p = Path(tmp) / f"p{i}.mp3"
                p.write_bytes(b"\xff\xfb" + b"\x00" * 100)
                parti.append(str(p))
            with patch.object(audio, "_ffmpeg", side_effect=FileNotFoundError("no ffmpeg")):
                with self.assertRaises(RuntimeError) as ctx:
                    audio._concat_audio(parti, str(Path(tmp) / "out.mp3"))
            self.assertIn("ffmpeg", str(ctx.exception).lower())


class CodecTests(unittest.TestCase):
    def test_encoder_bloccato_viene_scartato(self):
        """Regressione: il probe GPU codificava un 64x64 e passava anche su
        macchine dove l'encoder poi si impianta alla risoluzione reale,
        lasciando ffmpeg a `frame=0` per sempre (nessun timeout a valle)."""
        from unittest.mock import patch
        from moduli import montaggio

        def finto_run(cmd, **kw):
            from unittest.mock import MagicMock
            if "-encoders" in cmd:
                m = MagicMock(); m.stdout = "h264_nvenc h264_qsv"; return m
            if "h264_nvenc" in cmd:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
            m = MagicMock(); m.returncode = 0; return m

        with patch.object(montaggio, "_GPU_CODEC", None), \
             patch.object(montaggio.subprocess, "run", side_effect=finto_run):
            self.assertEqual(montaggio._detect_gpu_codec(), "h264_qsv")

    def test_video_codec_forzabile_da_env(self):
        from unittest.mock import patch
        from moduli import montaggio

        with patch.object(montaggio, "_GPU_CODEC", None), \
             patch.dict(os.environ, {"VIDEO_CODEC": "libx264"}):
            self.assertEqual(montaggio._detect_gpu_codec(), "libx264")

    def test_segmento_ha_un_timeout(self):
        """Senza timeout un encoder bloccato appendeva il daemon per sempre."""
        from moduli.montaggio import SEGMENT_TIMEOUT, FFMPEG_TIMEOUT

        self.assertGreater(SEGMENT_TIMEOUT, 0)
        self.assertGreater(FFMPEG_TIMEOUT, 0)


@unittest.skipUnless(_ha_ffmpeg(), "richiede ffmpeg")
class RenderTests(unittest.TestCase):
    """Render vero: e' l'unico modo per cogliere i fallimenti silenziosi di
    ffmpeg (exit 0 ma output sbagliato)."""

    @classmethod
    def setUpClass(cls):
        from moduli.ffmpeg_utils import ffmpeg_path
        from moduli import montaggio
        # codec fisso: qui interessano durata e caption, non la scelta del
        # codec (coperta da CodecTests) — ed evita 25s di probe GPU per test
        cls._codec_originale = montaggio._GPU_CODEC
        montaggio._GPU_CODEC = "libx264"
        cls.tmp = tempfile.mkdtemp()
        ff = ffmpeg_path()
        # clip da 8s: piu' corta della somma dei segmenti, cosi' il seek deve
        # avvolgersi e si riproduce la condizione del bug
        for nome, sorgente in (("clip1", "testsrc"), ("clip2", "smptebars")):
            subprocess.run(
                [ff, "-y", "-v", "error", "-f", "lavfi",
                 "-i", f"{sorgente}=size=320x180:rate=25:duration=8",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 os.path.join(cls.tmp, f"{nome}.mp4")], check=True)
        subprocess.run(
            [ff, "-y", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=300:duration=22",
             os.path.join(cls.tmp, "narrazione.mp3")], check=True)

    @classmethod
    def tearDownClass(cls):
        from moduli import montaggio
        montaggio._GPU_CODEC = cls._codec_originale
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_video_copre_tutta_la_narrazione(self):
        """Regressione: con `offset % durata_clip` il seek cadeva a ridosso
        della fine della clip, ffmpeg produceva segmenti piu' corti del
        richiesto e `-shortest` troncava la voce narrante nel mux."""
        from moduli.montaggio import _monta_video_ffmpeg, _media_duration

        clip = {"a": os.path.join(self.tmp, "clip1.mp4"),
                "b": os.path.join(self.tmp, "clip2.mp4")}
        audio = os.path.join(self.tmp, "narrazione.mp3")
        out = os.path.join(self.tmp, "finale.mp4")
        _monta_video_ffmpeg(audio, list(clip), clip, out, mood="epic",
                            captions_text="Don't stop, it's 50% faster and cheaper today.")

        durata_audio = _media_duration(audio)
        durata_video = _media_duration(out)
        self.assertAlmostEqual(durata_video, durata_audio, delta=0.6,
                               msg=f"video {durata_video}s vs audio {durata_audio}s")

    def test_segmento_con_caratteri_speciali_disegna_la_caption(self):
        """Un segmento senza caption pesa molto meno: se drawtext fallisce in
        silenzio la differenza di dimensione lo rivela."""
        from moduli.montaggio import _render_segment_ffmpeg

        src = os.path.join(self.tmp, "clip1.mp4")
        con = os.path.join(self.tmp, "con_caption.mp4")
        senza = os.path.join(self.tmp, "senza_caption.mp4")
        _render_segment_ffmpeg(src, con, 0, 2, "DON’T STOP, IT’S 50% FASTER", 8.0)
        _render_segment_ffmpeg(src, senza, 0, 2, None, 8.0)
        self.assertGreater(os.path.getsize(con), os.path.getsize(senza))


class ThumbnailTests(unittest.TestCase):
    def test_copertina_prodotta_con_dimensione_giusta(self):
        from moduli.thumbnail import _draw_title, _fetch_image_placeholder, THUMB_W, THUMB_H

        base = _fetch_image_placeholder("x", mood="epic")
        for posizione in ("alto", "basso"):
            img = _draw_title(base.copy(), "THE FUTURE OF AI", position=posizione)
            self.assertEqual(img.size, (THUMB_W, THUMB_H))

    def test_banda_scurisce_lo_sfondo_dietro_al_testo(self):
        """Regressione dal passaggio putpixel -> resize: la banda sfumata deve
        continuare a esistere, altrimenti il testo bianco su sfondo chiaro
        diventa illeggibile."""
        from PIL import Image
        from moduli.thumbnail import _draw_title, THUMB_W, THUMB_H

        chiaro = Image.new("RGB", (THUMB_W, THUMB_H), (240, 240, 240))
        img = _draw_title(chiaro.copy(), "TEST", position="basso")
        in_basso = img.getpixel((5, THUMB_H - 5))
        in_alto = img.getpixel((5, 5))
        self.assertLess(sum(in_basso), sum(in_alto),
                        "la banda sfumata in basso non e' stata disegnata")


if __name__ == "__main__":
    unittest.main()
