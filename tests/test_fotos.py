# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validação estrutural de JPEG: o que o decodificador sozinho deixaria passar."""
import unittest

from vestigo import fotos
from tests.sintetico import jpeg


class TestEstruturaJpeg(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.j = jpeg(640, 360, semente=1)

    def test_jpeg_integra(self):
        r = fotos.estrutura_jpeg(self.j)
        self.assertTrue(r["ok"], r["motivo"])
        self.assertEqual((r["largura"], r["altura"]), (640, 360))
        self.assertEqual(r["fim_eoi"], len(self.j))

    def test_trailer_depois_do_eoi_fica_de_fora(self):
        # como o .insp: JPEG seguido de metadados da câmera
        r = fotos.estrutura_jpeg(self.j + b"TRAILER" * 500)
        self.assertTrue(r["ok"])
        self.assertEqual(r["fim_eoi"], len(self.j))

    def test_lacuna_de_zeros_no_meio_e_recusada(self):
        # JPEG "partida" com 1 MiB de zeros no meio: o decodificador aceitaria calado
        meio = len(self.j) // 2
        partida = self.j[:meio] + bytes(1 << 20) + self.j[meio:]
        r = fotos.estrutura_jpeg(partida)
        self.assertFalse(r["ok"])
        self.assertIn("lacuna", r["motivo"])

    def test_zeros_em_segmento_app_nao_e_lacuna(self):
        # câmeras reais põem preenchimento de zeros em segmentos APP: não é defeito
        app = b"\xff\xe9" + (8192 + 2).to_bytes(2, "big") + bytes(8192)
        r = fotos.estrutura_jpeg(self.j[:2] + app + self.j[2:])
        self.assertTrue(r["ok"], r["motivo"])

    def test_sem_eoi(self):
        r = fotos.estrutura_jpeg(self.j[:len(self.j) // 2])
        self.assertFalse(r["ok"])

    def test_nao_e_jpeg(self):
        self.assertFalse(fotos.estrutura_jpeg(b"\x00" * 100)["ok"])


if __name__ == "__main__":
    unittest.main()
