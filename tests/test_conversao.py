# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Conversões: o remux não pode alterar nenhum pacote; o JPG sai sem o trailer da câmera."""
import os
import subprocess
import tempfile
import unittest

from vestigo import conversao
from vestigo.recuperacao import achar_ffmpeg
from tests.sintetico import jpeg

FFMPEG, FFPROBE = achar_ffmpeg()


def pacotes(arquivo, fluxo):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", arquivo, "-map", fluxo, "-c", "copy",
                        "-f", "framemd5", "-"], capture_output=True, text=True)
    return [l.rsplit(",", 1)[-1].strip() for l in r.stdout.splitlines() if l and not l.startswith("#")]


@unittest.skipUnless(FFMPEG, "FFmpeg não encontrado")
class TestConversao(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # vídeo "de câmera 360°": duas trilhas quadradas (uma por lente) e áudio, extensão .insv
        cls.insv = os.path.join(cls.tmp.name, "camera.insv")
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x256:d=1",
                        "-f", "lavfi", "-i", "testsrc=s=256x256:d=1", "-f", "lavfi",
                        "-i", "sine=d=1", "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264",
                        "-c:a", "aac", "-f", "mp4", cls.insv], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_opcoes_para_duas_lentes(self):
        chaves = [o[0] for o in conversao.opcoes(self.insv, FFPROBE)]
        self.assertEqual(chaves, ["mp4", "lado_a_lado", "equiretangular"])

    def test_remux_copia_todos_os_pacotes(self):
        r = conversao.converter(self.insv, "mp4", os.path.join(self.tmp.name, "out"), FFMPEG, FFPROBE)
        self.assertFalse(r["recodifica"])
        for fluxo in ("0:v:0", "0:v:1", "0:a:0"):
            self.assertEqual(pacotes(self.insv, fluxo), pacotes(r["arquivo"], fluxo), fluxo)

    def test_lado_a_lado_recodifica_e_avisa(self):
        r = conversao.converter(self.insv, "lado_a_lado", os.path.join(self.tmp.name, "out"),
                                FFMPEG, FFPROBE, largura=512)
        self.assertTrue(r["recodifica"])
        self.assertIn("CÓPIA DE VISUALIZAÇÃO", r["observacao"])
        self.assertTrue(os.path.getsize(r["arquivo"]) > 0)

    def test_jpg_sem_trailer(self):
        j = jpeg(320, 160, semente=5)
        insp = os.path.join(self.tmp.name, "foto.insp")
        with open(insp, "wb") as f:
            f.write(j + b"TRAILER" * 100)
        r = conversao.converter(insp, "jpg", os.path.join(self.tmp.name, "out"), FFMPEG, FFPROBE)
        with open(r["arquivo"], "rb") as f:
            self.assertEqual(f.read(), j)


if __name__ == "__main__":
    unittest.main()
