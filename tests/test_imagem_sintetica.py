# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ponta a ponta sobre uma imagem dd sintética: varredura, hashes, carving e relatório."""
import hashlib
import json
import os
import tempfile
import unittest

from vestigo.recuperacao import Recuperador
from vestigo.sessao import Sessao
from vestigo.varredura import Varredura
from tests.sintetico import imagem_dd, jpeg

MiB = 1 << 20


class TestImagemSintetica(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.a = jpeg(1280, 720, semente=2)
        cls.b = jpeg(800, 600, semente=3)
        c = jpeg(960, 540, semente=4)
        meio = len(c) // 2
        cls.dd = imagem_dd(os.path.join(d, "sintetica.dd"), 64 * MiB, [
            (1 * MiB, cls.a),                                  # contígua
            (8 * MiB, cls.b + b"TRAILER" * 400),              # contígua, com trailer (tipo .insp)
            (20 * MiB, c[:meio]), (40 * MiB, c[meio:]),       # fragmentada: não pode sair "íntegra"
        ])
        cls.saida = os.path.join(d, "saida")
        cls.sessao = Sessao(cls.dd)
        s = cls.sessao
        cls.var = Varredura(s.img, s.busca).executar()
        cls.itens = Recuperador(s.img, s.vol, cls.var, cls.saida, log=lambda *_: None).executar()

    @classmethod
    def tearDownClass(cls):
        cls.sessao.img.fechar()
        cls.tmp.cleanup()

    def test_hash_da_varredura_e_o_do_arquivo(self):
        with open(self.dd, "rb") as f:
            self.assertEqual(self.var.md5, hashlib.md5(f.read()).hexdigest())
        self.assertFalse(self.var.hash_incompleto)

    def test_duas_fotos_contiguas_recuperadas_exatas(self):
        fotos = sorted((it for it in self.itens if it["origem"] == "foto carving"), key=lambda it: it["arquivo"])
        self.assertEqual(len(fotos), 2)
        for it, original in zip(fotos, (self.a, self.b)):
            with open(it["arquivo"], "rb") as f:
                self.assertEqual(f.read(), original)          # byte a byte, sem o trailer
            self.assertTrue(it["estado"].startswith("íntegra"), it["estado"])

    def test_fragmentada_nao_sai_como_integra(self):
        self.assertFalse(any(it["faixas"][0][0] == 20 * MiB for it in self.itens))

    def test_relatorio_tem_sha256_de_cada_arquivo(self):
        with open(os.path.join(self.saida, "relatorio.json"), encoding="utf-8") as f:
            rel = json.load(f)
        for it in rel["itens"]:
            with open(it["arquivo"], "rb") as f:
                self.assertEqual(it["sha256"], hashlib.sha256(f.read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
