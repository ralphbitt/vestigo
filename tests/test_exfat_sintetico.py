# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""exFAT sintético: fotos viva, apagada e fragmentada (pela cadeia da FAT) e apagada zerada."""
import os
import tempfile
import unittest

from vestigo.recuperacao import Recuperador
from vestigo.sessao import Sessao
from vestigo.varredura import Varredura
from tests.sintetico import exfat, jpeg

CL = 32 << 10


class TestExFATSintetico(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.viva, cls.apagada, zerada = (jpeg(1600, 1200, semente=s) for s in (7, 8, 9))
        n = -(-len(cls.apagada) // CL)
        frag = list(range(40, 40 + n // 2)) + list(range(300, 300 + n - n // 2))
        img = exfat(os.path.join(cls.tmp.name, "cartao.dd"), [
            {"nome": "DCIM", "pasta": True},
            {"nome": "IMG_0001.JPG", "pasta": "DCIM", "dados": cls.viva,
             "clusters": list(range(8, 8 + -(-len(cls.viva) // CL)))},
            {"nome": "IMG_0002.JPG", "pasta": "DCIM", "dados": cls.apagada, "clusters": frag, "apagado": True},
            {"nome": "IMG_0003.JPG", "pasta": "DCIM", "dados": zerada, "apagado": True, "zerado": True,
             "clusters": list(range(500, 500 + -(-len(zerada) // CL)))},
        ], tamanho=32 << 20)
        cls.sessao = Sessao(img)
        s = cls.sessao
        var = Varredura(s.img, s.busca).executar()
        cls.rec = Recuperador(s.img, s.vol, var, os.path.join(cls.tmp.name, "saida"), log=lambda *_: None)
        cls.itens = cls.rec.executar()

    @classmethod
    def tearDownClass(cls):
        cls.sessao.img.fechar()
        cls.tmp.cleanup()

    def conteudo(self, origem):
        it = next(it for it in self.itens if it["origem"] == origem)
        with open(it["arquivo"], "rb") as f:
            return f.read()

    def test_volume_lido(self):
        self.assertEqual(self.sessao.vol.tipo, "exFAT")
        nomes = {a.nome for a in self.sessao.vol.arquivos.values()}
        self.assertTrue({"IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.JPG"} <= nomes)

    def test_viva_e_apagada_fragmentada_exatas(self):
        self.assertEqual(self.conteudo("foto viva"), self.viva)
        self.assertEqual(self.conteudo("foto apagada"), self.apagada)

    def test_zerada_nao_recuperada_com_motivo(self):
        motivos = {os.path.basename(n["arquivo"]): n["motivo"] for n in self.rec.nao_recuperados}
        self.assertIn("zeros", motivos.get("IMG_0003.JPG", ""))


if __name__ == "__main__":
    unittest.main()
