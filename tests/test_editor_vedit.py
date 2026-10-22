"""Montaggio con vedit (`VIDEO_EDITOR=vedit`).

Coprono la scelta dell'editor, il ripiego sul montaggio interno quando vedit
manca o fallisce (vedit non deve mai far saltare un video), le decisioni di
montaggio scritte nella timeline (clip AI in testa, durata pari alla
narrazione, transizioni, ducking, testi) e un render reale. I test che
richiedono il pacchetto `vedit` o ffmpeg si saltano da soli se mancano.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from moduli import editor_vedit, montaggio


def _ha_ffmpeg() -> bool:
    from moduli.ffmpeg_utils import ffmpeg_path
    try:
        ffmpeg_path()
        return True
    except FileNotFoundError:
        return False


class _EnvPulito(unittest.TestCase):
    VARIABILI = ("VIDEO_EDITOR", "VEDIT_LOOK", "VEDIT_TRANSIZIONE",
                 "VEDIT_TRANSIZIONE_DURATA", "VEDIT_TRANSIZIONE_OGNI",
                 "VEDIT_KENBURNS", "VEDIT_QUALITA")

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


class SceltaEditorTests(_EnvPulito):
    def test_default_resta_interno(self):
        self.assertEqual(editor_vedit.editore(), "interno")
        self.assertFalse(editor_vedit.attivo())

    def test_valore_sconosciuto_non_rompe_la_pipeline(self):
        os.environ["VIDEO_EDITOR"] = "premiere"
        self.assertEqual(editor_vedit.editore(), "interno")

    def test_vedit_attivo(self):
        os.environ["VIDEO_EDITOR"] = " Vedit "
        self.assertTrue(editor_vedit.attivo())


class RipiegoTests(_EnvPulito):
    def test_errore_vedit_ripiega_sul_montaggio_interno(self):
        os.environ["VIDEO_EDITOR"] = "vedit"
        with patch.object(editor_vedit, "monta_video_vedit",
                          side_effect=RuntimeError("filtro rotto")) as v, \
             patch.object(montaggio, "_FFMPEG_ONLY", True), \
             patch.object(montaggio, "_monta_video_ffmpeg") as interno:
            montaggio.monta_video("a.mp3", ["k"], {"k": "c.mp4"}, "out.mp4", mood="epic")
        v.assert_called_once()
        interno.assert_called_once()

    def test_vedit_riuscito_non_chiama_il_montaggio_interno(self):
        os.environ["VIDEO_EDITOR"] = "vedit"
        with patch.object(editor_vedit, "monta_video_vedit") as v, \
             patch.object(montaggio, "_FFMPEG_ONLY", True), \
             patch.object(montaggio, "_monta_video_ffmpeg") as interno:
            montaggio.monta_video("a.mp3", ["k"], {"k": "c.mp4"}, "out.mp4")
        v.assert_called_once()
        interno.assert_not_called()

    def test_editor_interno_non_tocca_vedit(self):
        with patch.object(editor_vedit, "monta_video_vedit") as v, \
             patch.object(montaggio, "_FFMPEG_ONLY", True), \
             patch.object(montaggio, "_monta_video_ffmpeg") as interno:
            montaggio.monta_video("a.mp3", ["k"], {"k": "c.mp4"}, "out.mp4")
        v.assert_not_called()
        interno.assert_called_once()

    def test_pacchetto_assente_solleva_errore_chiaro(self):
        with patch.object(editor_vedit, "disponibile", return_value=False):
            with self.assertRaises(editor_vedit.VeditNonDisponibile) as ctx:
                editor_vedit.monta_video_vedit("a.mp3", ["k"], {"k": "c.mp4"}, "out.mp4")
        self.assertIn("[editor]", str(ctx.exception))


class ImpostazioniTests(_EnvPulito):
    def test_menu_espone_tutti_gli_editor(self):
        from moduli import impostazioni_video
        ids = {e["id"] for e in impostazioni_video.EDITORI.values()}
        self.assertEqual(ids, set(editor_vedit.EDITORI))

    def test_scelta_scritta_nel_env(self):
        from io import StringIO
        from pathlib import Path
        from rich.console import Console
        from moduli import impostazioni_video

        with tempfile.TemporaryDirectory() as tmp, \
             patch("rich.prompt.Prompt.ask", return_value="2"):
            scelta = impostazioni_video.configura_editor(Console(file=StringIO()), tmp)
            self.assertEqual(scelta, "vedit")
            self.assertIn("VIDEO_EDITOR=vedit", Path(tmp, ".env").read_text(encoding="utf-8"))

    def test_stato_segnala_pacchetto_mancante(self):
        from pathlib import Path
        from moduli import impostazioni_video

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("VIDEO_EDITOR=vedit\n", encoding="utf-8")
            with patch.object(editor_vedit, "disponibile", return_value=False):
                self.assertIn("mancante", impostazioni_video.stato_editor(tmp))
            Path(tmp, ".env").write_text("", encoding="utf-8")
            self.assertEqual(impostazioni_video.stato_editor(tmp), "interno")


class PezziTests(unittest.TestCase):
    def test_sorgente_lunga_un_solo_tratto_dentro_la_clip(self):
        for offset in (0, 5, 35, 120):
            (attacco, d), = editor_vedit._pezzi(12.0, offset, 5.0)
            self.assertEqual(d, 5.0)
            self.assertLessEqual(attacco + d, 12.0 + 1e-6)

    def test_sorgente_corta_si_ripete_fino_a_coprire(self):
        pezzi = editor_vedit._pezzi(2.0, 10, 5.0)
        self.assertAlmostEqual(sum(d for _, d in pezzi), 5.0)
        self.assertTrue(all(a == 0.0 and d <= 2.0 for a, d in pezzi))


@unittest.skipUnless(editor_vedit.disponibile() and _ha_ffmpeg(), "richiede vedit e ffmpeg")
class TimelineTests(_EnvPulito):
    """Timeline costruita davvero (ffprobe sui media), senza render."""

    @classmethod
    def setUpClass(cls):
        from moduli.ffmpeg_utils import ffmpeg_path
        cls.tmp = tempfile.mkdtemp()
        ff = ffmpeg_path()
        for nome, sorgente in (("clip1", "testsrc"), ("clip2", "smptebars"), ("ai1", "rgbtestsrc")):
            subprocess.run(
                [ff, "-y", "-v", "error", "-f", "lavfi",
                 "-i", f"{sorgente}=size=320x180:rate=25:duration=8",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 os.path.join(cls.tmp, f"{nome}.mp4")], check=True)
        subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=300:duration=22",
                        os.path.join(cls.tmp, "narrazione.mp3")], check=True)
        subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=500:duration=9",
                        os.path.join(cls.tmp, "musica.mp3")], check=True)
        cls.clip = {"a": os.path.join(cls.tmp, "clip1.mp4"),
                    "b": os.path.join(cls.tmp, "clip2.mp4"),
                    f"{montaggio.PREFISSO_CLIP_AI}scena": os.path.join(cls.tmp, "ai1.mp4")}
        cls.audio = os.path.join(cls.tmp, "narrazione.mp3")
        cls.musica = os.path.join(cls.tmp, "musica.mp3")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _progetto(self, **kw):
        return editor_vedit.costruisci_progetto(
            self.audio, list(self.clip), self.clip, mood=kw.get("mood", "epic"),
            custom_music=kw.get("musica", self.musica),
            captions_text=kw.get("testo", "This battery charges in five minutes and lasts a week."))

    def _traccia(self, st, tid):
        return st.project.track_by_id(tid)

    def test_video_lungo_quanto_la_narrazione(self):
        st = self._progetto()
        fine = max(c.end for c in self._traccia(st, "V1").clips)
        voce = self._traccia(st, "A1").clips
        self.assertEqual(len(voce), 1)
        self.assertAlmostEqual(voce[0].duration, montaggio._media_duration(self.audio), delta=0.05)
        # la coda di una transizione non puo' allungare il video
        self.assertAlmostEqual(fine, voce[0].duration, delta=0.05)

    def test_clip_ai_sul_primo_segmento(self):
        st = self._progetto()
        prima = min(self._traccia(st, "V1").clips, key=lambda c: c.start)
        media = st.project.media_by_id(prima.media)
        self.assertTrue(media.path.endswith("ai1.mp4"), media.path)

    def test_clip_coprono_lo_schermo_e_sono_mute(self):
        st = self._progetto()
        for c in self._traccia(st, "V1").clips:
            self.assertEqual(c.fit, "cover")
            self.assertTrue(c.audio.mute)

    def test_transizioni_ogni_n_stacchi(self):
        os.environ["VEDIT_TRANSIZIONE_OGNI"] = "2"
        st = self._progetto()
        con = [c for c in self._traccia(st, "V1").clips
               if c.fade_out > 0 or c.transition_out.duration > 0]
        self.assertTrue(con)
        os.environ["VEDIT_TRANSIZIONE"] = "nessuna"
        st = self._progetto()
        self.assertFalse([c for c in self._traccia(st, "V1").clips
                          if c.fade_out > 0 or c.transition_out.duration > 0])

    def test_musica_in_loop_con_ducking_sulla_voce(self):
        st = self._progetto()
        musica = [t for t in st.project.tracks if t.kind == "audio" and t.id != "A1"]
        self.assertEqual(len(musica), 1)
        self.assertEqual(musica[0].sidechain["source"], "A1")
        clip = musica[0].clips
        # brano da 9s su 22s di narrazione: deve ripetersi fino in fondo
        self.assertGreaterEqual(len(clip), 3)
        self.assertAlmostEqual(max(c.end for c in clip), montaggio._media_duration(self.audio),
                               delta=0.05)

    def test_testi_chiave_su_traccia_sopra(self):
        st = self._progetto()
        video = st.project.video_tracks()
        self.assertGreaterEqual(len(video), 2)
        testi = [c for c in video[-1].clips if c.type == "text"]
        self.assertTrue(testi)

    def test_look_dal_mood_e_disattivabile(self):
        st = self._progetto(mood="epic")
        self.assertTrue(self._traccia(st, "V1").clips[0].effects)
        os.environ["VEDIT_LOOK"] = "nessuno"
        st = self._progetto(mood="epic")
        self.assertFalse(self._traccia(st, "V1").clips[0].effects)

    def test_loudnorm_al_target_del_canale(self):
        st = self._progetto()
        ln = st.project.master.loudnorm
        self.assertTrue(ln.enabled)
        self.assertEqual(ln.i, montaggio.LOUDNESS_TARGET_LUFS)

    def test_render_reale(self):
        os.environ["VEDIT_QUALITA"] = "draft"
        out = os.path.join(self.tmp, "vedit_finale.mp4")
        visti = []
        editor_vedit.monta_video_vedit(self.audio, list(self.clip), self.clip, out,
                                       mood="epic", custom_music=self.musica,
                                       on_progress=lambda p, e: visti.append(p),
                                       captions_text="This battery charges in five minutes.")
        self.assertAlmostEqual(montaggio._media_duration(out),
                               montaggio._media_duration(self.audio), delta=0.6)
        self.assertEqual(visti[-1], 100)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "progetto_vedit.json")))


if __name__ == "__main__":
    unittest.main()
