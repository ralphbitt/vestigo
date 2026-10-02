# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Leitura do exFAT direto da imagem (cartões SD de câmeras, pendrives grandes).

Expõe a mesma interface de ntfs.VolumeNTFS, para o mapa e a recuperação não precisarem saber
qual é o sistema de arquivos. Os "LCN" são os números de cluster do exFAT: o volume é
posicionado de modo que  offset + cluster × tamanho_do_cluster  seja o endereço do cluster.

Arquivos apagados: o exFAT marca a entrada de diretório como livre (0x85 -> 0x05) e libera o
bitmap, mas normalmente preserva a cadeia na FAT. Enquanto os clusters não forem reutilizados,
o arquivo inteiro sai seguindo a cadeia — mesmo fragmentado, que é o caso típico de câmeras
que gravam vídeo e proxy ao mesmo tempo.
"""
import datetime
import struct

import numpy as np

from .ntfs import Arquivo, EXT_VIDEO


def _dos(ts, cs=0):
    if not ts:
        return None
    try:
        d = datetime.datetime(1980 + (ts >> 25), (ts >> 21) & 0xF, (ts >> 16) & 0x1F,
                              (ts >> 11) & 0x1F, (ts >> 5) & 0x3F, (ts & 0x1F) * 2)
        return (d + datetime.timedelta(milliseconds=cs * 10)).isoformat(sep=" ", timespec="seconds")
    except ValueError:
        return None


class ArquivoExFAT(Arquivo):
    """Entrada de diretório exFAT com a cadeia de clusters convertida em runs."""

    def __init__(self, num, nome, pai, caminho, em_uso, eh_dir, real, init, runs, criado, modificado,
                 meta=False, cadeia_completa=True):
        self.num, self.nome, self.pai, self.caminho = num, nome, pai, caminho
        self.em_uso, self.dir = em_uso, eh_dir
        self.real, self.init, self.runs = real, init, runs
        self.alloc = 0
        self.criado, self.modificado = criado, modificado
        self.indice_runs, self.ads = [], []
        self._meta = meta
        self.cadeia_completa = cadeia_completa

    @property
    def metadado(self):
        return self._meta


class VolumeExFAT:
    tipo = "exFAT"

    def __init__(self, img, offset=0):
        self.img, self.inicio = img, offset
        b = img.ler(offset, 512)
        if b[3:11] != b"EXFAT   ":
            raise ValueError("não há exFAT no offset %d" % offset)
        self.bps = 1 << b[108]
        self.cluster = self.bps << b[109]
        self.fat_off = offset + struct.unpack("<I", b[80:84])[0] * self.bps
        self.fat_len = struct.unpack("<I", b[84:88])[0] * self.bps
        self.heap = offset + struct.unpack("<I", b[88:92])[0] * self.bps
        self.n_clusters = struct.unpack("<I", b[92:96])[0]
        self.raiz = struct.unpack("<I", b[96:100])[0]
        self.tamanho = struct.unpack("<Q", b[72:80])[0] * self.bps
        self.offset = self.heap - 2 * self.cluster        # cluster c -> offset + c * cluster
        self.reg_tam = 32
        self.fat = np.frombuffer(img.ler(self.fat_off, (self.n_clusters + 2) * 4), dtype="<u4")
        self.arquivos = {}
        self.bitmap = None
        self._bitmap_ent = None
        self._n = 0
        self._meta = []
        self._varrer_dir(self.raiz, False, 0, "", 5, True, 0)
        self._metadados()
        self._ler_bitmap()

    # ------------------------------------------------------------------ clusters
    def cadeia(self, c, contiguo, n):
        """Clusters de um arquivo: contíguos (NoFatChain) ou seguindo a FAT."""
        if contiguo:
            return list(range(c, c + n))
        out, vistos = [], set()
        while 2 <= c < self.n_clusters + 2 and len(out) < n and c not in vistos:
            vistos.add(c)
            out.append(c)
            c = int(self.fat[c])
        return out

    @staticmethod
    def _runs(clusters):
        runs = []
        for c in clusters:
            if runs and runs[-1][0] + runs[-1][1] == c:
                runs[-1][1] += 1
            else:
                runs.append([c, 1])
        return [(a, n) for a, n in runs]

    def ler_runs(self, runs, limite=None):
        dado = b"".join(self.img.ler(self.offset + c * self.cluster, n * self.cluster) for c, n in runs)
        return dado[:limite] if limite else dado

    # ------------------------------------------------------------------ diretórios
    def _varrer_dir(self, c, contiguo, tam, caminho, pai, em_uso_pai, prof):
        n = max(1, -(-tam // self.cluster)) if tam else 256
        clus = self.cadeia(c, contiguo, n)
        self._meta.append(self._runs(clus)) if em_uso_pai else None
        d = self.ler_runs(self._runs(clus))
        i = 0
        while i + 32 <= len(d):
            t = d[i]
            if t == 0:
                break
            if t == 0x81 and em_uso_pai and prof == 0:
                self._bitmap_ent = struct.unpack("<IQ", d[i + 20:i + 32])
            elif t == 0x82 and prof == 0:
                pc, ln = struct.unpack("<IQ", d[i + 20:i + 32])
                self._meta.append(self._runs(self.cadeia(pc, False, -(-ln // self.cluster))))
            if t in (0x85, 0x05):
                sec = d[i + 1]
                ent = [d[i + 32 * k:i + 32 * k + 32] for k in range(1, sec + 1)]
                if ent and len(ent[0]) == 32 and ent[0][0] & 0x7F == 0x40:
                    self._entrada(d[i:i + 32], ent, t == 0x85 and em_uso_pai, caminho, pai, prof)
                i += 32 * (sec + 1)
                continue
            i += 32

    def _entrada(self, fe, ent, em_uso, caminho, pai, prof):
        attr = struct.unpack("<H", fe[4:6])[0]
        criado = _dos(struct.unpack("<I", fe[8:12])[0], fe[20])
        modif = _dos(struct.unpack("<I", fe[12:16])[0], fe[21])
        st = ent[0]
        contiguo = bool(st[1] & 2)
        nl = st[3]
        init, primeiro, real = struct.unpack("<Q", st[8:16])[0], *struct.unpack("<IQ", st[20:32])
        nome = b"".join(e[2:32] for e in ent[1:] if e and e[0] & 0x7F == 0x41).decode(
            "utf-16-le", "replace")[:nl]
        if not nome:
            return
        precisa = -(-real // self.cluster) if real else 0
        clus = self.cadeia(primeiro, contiguo, precisa) if primeiro >= 2 else []
        eh_dir = bool(attr & 0x10)
        self._n += 1
        num = 100000 + self._n
        cam = (caminho + "\\" + nome).lstrip("\\")
        a = ArquivoExFAT(num, nome, pai, cam, em_uso, eh_dir, real, min(init, real) if init else real,
                         self._runs(clus), criado, modif, cadeia_completa=len(clus) >= precisa)
        self.arquivos[num] = a
        if eh_dir and primeiro >= 2 and prof < 8:
            self._varrer_dir(primeiro, contiguo, real, cam, num, em_uso, prof + 1)

    def _metadados(self):
        """Região da FAT e estruturas do volume como um 'arquivo' de metadados, para o mapa."""
        fat_c = (self.fat_off - self.offset) // self.cluster
        runs = [(fat_c, -(-self.fat_len // self.cluster))]
        if self._bitmap_ent:
            pc, ln = self._bitmap_ent
            runs += self._runs(self.cadeia(pc, False, -(-ln // self.cluster)))
        for r in self._meta:
            runs += r
        self.arquivos[1] = ArquivoExFAT(1, "$exFAT: FAT, bitmap, tabela de maiúsculas, diretórios", 5,
                                        "", True, False, 0, 0, runs, None, None, meta=True)

    def _ler_bitmap(self):
        if not self._bitmap_ent:
            return
        pc, ln = self._bitmap_ent
        bits = np.frombuffer(self.ler_runs(self._runs(self.cadeia(pc, False, -(-ln // self.cluster))), ln),
                             dtype=np.uint8)
        b = np.unpackbits(bits, bitorder="little")[:self.n_clusters].astype(bool)
        self.bitmap = np.concatenate(([True, True], b))           # clusters 0 e 1 não existem

    def livres(self):
        if self.bitmap is None:
            return []
        b = np.concatenate(([True], self.bitmap, [True]))
        d = np.diff(b.astype(np.int8))
        ini, fim = np.nonzero(d == -1)[0], np.nonzero(d == 1)[0]
        return [(int(i) * self.cluster, int(f - i) * self.cluster) for i, f in zip(ini, fim)]
