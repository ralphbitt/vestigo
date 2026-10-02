# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Varredura sequencial da imagem inteira, do setor 0 ao último.

Numa única passada:
  - calcula MD5 e SHA-1 do dispositivo (para conferir com a aquisição);
  - classifica cada unidade de 4 KiB como zeros, 0xFF (NAND apagada) ou dados, para o mapa;
  - registra assinaturas de vídeo:
      * 'ftyp' em início de setor  -> candidato a MP4/MOV completo (carving);
      * delimitador de quadro H.264 em formato AVCC (00 00 00 02 09) fora de arquivos
        íntegros -> candidato a fluxo de vídeo sem índice.
"""
import hashlib
import queue
import re
import threading
import time

import numpy as np

UNIDADE = 4096
BLOCO = 16 * 1024 * 1024
RE_AUD = re.compile(rb"\x00\x00\x00\x02\x09[\x10\x30\x50\xf0]")
RE_JPEG = re.compile(rb"\xff\xd8\xff[\xc4\xdb\xdd\xe0-\xef\xfe]")   # APPn, DQT, DHT, DRI, COM
RE_FTYP = re.compile(rb"ftyp(?:isom|iso2|iso4|iso5|iso6|mp41|mp42|avc1|qt  |M4V |M4A |3gp4|3gp5|3gp6|3g2a|XAVC|dash|MSNV|crx |hvc1|heic|NDAS)")


class Varredura:
    def __init__(self, img, intervalos_busca, n_baldes=2400):
        """intervalos_busca: [(ini, fim)] em bytes absolutos onde procurar fluxos sem índice
        (espaço livre, faixas não inicializadas, áreas fora do sistema de arquivos)."""
        self.img = img
        self.total = img.tamanho
        self.busca = sorted(intervalos_busca)
        self.n_baldes = n_baldes
        self.por_balde = -(-self.total // n_baldes)
        self.zeros = np.zeros(n_baldes, np.int64)
        self.ff = np.zeros(n_baldes, np.int64)
        self.dados = np.zeros(n_baldes, np.int64)
        self.video = np.zeros(n_baldes, np.int64)
        self.ilegivel = np.zeros(n_baldes, np.int64)
        self.pos = 0
        self.ftyp = []                 # offsets absolutos
        self.jpeg = []                 # início de JPEG em início de setor
        self.aud = []                  # offsets absolutos (arrays por bloco)
        self.md5 = self.sha1 = None
        self.hash_incompleto = False          # houve trecho ilegível: o hash não é o do dispositivo
        self.velocidade = 0.0
        self.parar = False
        self.concluida = False

    # --------------------------------------------------------------------------------
    def _classificar(self, ini, blk):
        n = len(blk) // UNIDADE
        if n == 0:
            return
        a = np.frombuffer(blk, np.uint8, n * UNIDADE).reshape(n, UNIDADE)
        mx, mn = a.max(axis=1), a.min(axis=1)
        z, f = mx == 0, mn == 255
        d = ~(z | f)
        baldes = (ini + np.arange(n, dtype=np.int64) * UNIDADE) // self.por_balde
        np.add.at(self.zeros, baldes, z)
        np.add.at(self.ff, baldes, f)
        np.add.at(self.dados, baldes, d)

    def _assinaturas(self, ini, blk):
        for m in RE_FTYP.finditer(blk):
            o = ini + m.start() - 4
            if o % 512 == 0:
                self.ftyp.append(o)
                self.video[min(o // self.por_balde, self.n_baldes - 1)] += 1
        for m in RE_JPEG.finditer(blk):
            o = ini + m.start()
            if o % 512 == 0:
                self.jpeg.append(o)
                self.video[min(o // self.por_balde, self.n_baldes - 1)] += 1
        fim = ini + len(blk)
        for a, b in self.busca:
            if b <= ini or a >= fim:
                continue
            s, e = max(a, ini) - ini, min(b, fim) - ini
            hits = [ini + m.start() for m in RE_AUD.finditer(blk, s, e)]   # start() já é do bloco
            if hits:
                h = np.asarray(hits, np.int64)
                self.aud.append(h)
                np.add.at(self.video, np.minimum(h // self.por_balde, self.n_baldes - 1), 1)

    # --------------------------------------------------------------------------------
    def executar(self, ao_progredir=None):
        fila = queue.Queue(maxsize=4)
        md5, sha1 = hashlib.md5(), hashlib.sha1()

        def hasher():
            while True:
                b = fila.get()
                if b is None:
                    return
                md5.update(b)
                sha1.update(b)

        th = threading.Thread(target=hasher, daemon=True)
        th.start()
        t0 = time.time()
        pos = 0
        try:
            while pos < self.total and not self.parar:
                n_falhas = len(self.img.falhas)
                blk = self.img.ler(pos, BLOCO)
                if not blk:
                    break
                fila.put(blk)
                self._classificar(pos, blk)
                self._assinaturas(pos, blk)
                for a, b in self.img.falhas[max(n_falhas - 1, 0):]:
                    a, b = max(a, pos), min(b, pos + len(blk))
                    for o in range(a - a % UNIDADE, b, UNIDADE):
                        self.ilegivel[min(o // self.por_balde, self.n_baldes - 1)] += 1
                pos += len(blk)
                self.pos = pos
                self.velocidade = pos / max(time.time() - t0, 1e-6)
                if ao_progredir:
                    ao_progredir(self)
        finally:
            fila.put(None)
            th.join()
        if pos >= self.total:
            self.md5, self.sha1 = md5.hexdigest(), sha1.hexdigest()
            self.hash_incompleto = bool(self.img.falhas)
            self.concluida = True
        self.aud = np.concatenate(self.aud) if len(self.aud) else np.zeros(0, np.int64)
        return self
