# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acesso somente-leitura a imagens forenses: E01 (EWF) e dd/raw, inteiras ou segmentadas.

O perito passa o PRIMEIRO arquivo da imagem (evidencia.E01, disco.001, disco.dd); os demais
segmentos são localizados automaticamente pelo nome.
"""
import glob
import os
import re
import struct

PEDACO = 64 * 1024              # granularidade para isolar trechos ilegíveis


class ImagemIlegivel(Exception):
    """A imagem abre, mas não entrega dados: segmento truncado, faltando ou aquisição interrompida."""


def diagnosticar_ewf(caminho, segmentos, tamanho_declarado):
    """Percorre as seções do 1º segmento EWF e explica por que ele não entrega dados."""
    tipos = []
    tam = os.path.getsize(caminho)
    with open(caminho, "rb") as f:
        o = 13
        while o + 32 <= tam and len(tipos) < 500:
            f.seek(o)
            d = f.read(32)
            tipos.append(d[:16].rstrip(b"\0").decode("ascii", "replace"))
            prox = struct.unpack("<Q", d[16:24])[0]
            if prox <= o or prox > tam:
                break
            o = prox
    linhas = ["Seções do 1º segmento: %s." % ", ".join(tipos[:12]) + (" …" if len(tipos) > 12 else "")]
    if not {"sectors", "table", "data"} & set(tipos):
        linhas.append("Não há nenhuma seção de dados ('sectors'/'table') neste segmento: ele contém só o "
                      "cabeçalho da aquisição.")
    if tipos and tipos[-1] == "next":
        linhas.append("A última seção é 'next', que remete ao segmento seguinte (.E02), mas foram "
                      "encontrados %d segmento(s)." % len(segmentos))
    linhas.append("Tamanho declarado da mídia: %s bytes; tamanho do arquivo: %s bytes." % (
        format(tamanho_declarado, ",").replace(",", "."), format(tam, ",").replace(",", ".")))
    linhas.append("Causas prováveis: aquisição interrompida, ou cópia incompleta do arquivo .E01 "
                  "(ou dos demais segmentos). Confira com o log do equipamento de aquisição.")
    return "\n".join(linhas)


class Imagem:
    """Leitor de imagem com leitura aleatória. Nunca abre arquivo para escrita.

    Trechos que o leitor não consegue entregar (bloco EWF corrompido, segmento faltando) são
    devolvidos como zeros e registrados em self.falhas, para que a varredura siga adiante e o
    relatório declare o que não foi lido."""

    def __init__(self, caminho):
        self.caminho = os.path.abspath(caminho)
        if not os.path.isfile(self.caminho):
            raise FileNotFoundError(self.caminho)
        self.tipo = None
        self.segmentos = []
        self.hashes_gravados = {}
        self.falhas = []            # [(inicio, fim)] em bytes absolutos
        self._ewf = None
        self._raw = []              # [(inicio, fim, caminho, handle)]
        ext = os.path.splitext(self.caminho)[1].lower()
        if ext in (".e01", ".ex01", ".s01") or self._eh_ewf():
            self._abrir_ewf()
        else:
            self._abrir_raw()

    # ------------------------------------------------------------------ EWF
    def _eh_ewf(self):
        with open(self.caminho, "rb") as f:
            return f.read(8) in (b"EVF\x09\x0d\x0a\xff\x00", b"EVF2\r\n\x81\x00")

    def _abrir_ewf(self):
        import pyewf
        self.tipo = "EWF"
        self.segmentos = pyewf.glob(self.caminho)
        self._ewf = pyewf.handle()
        self._ewf.open(self.segmentos)
        self.tamanho = self._ewf.get_media_size()
        try:
            self.hashes_gravados = {k.upper().replace("SHA1", "SHA-1"): v.lower()
                                    for k, v in (self._ewf.get_hash_values() or {}).items()}
        except Exception:
            self.hashes_gravados = {}
        try:                                        # a imagem entrega o primeiro setor?
            self._ewf.seek(0)
            self._ewf.read(512)
        except OSError as e:
            raise ImagemIlegivel("A imagem abriu, mas o primeiro setor não pode ser lido.\n\n%s\n\n"
                                 "Mensagem do libewf: %s" % (
                                     diagnosticar_ewf(self.caminho, self.segmentos, self.tamanho),
                                     str(e).split(".")[0])) from None

    # ------------------------------------------------------------------ raw / dd
    def _abrir_raw(self):
        self.tipo = "RAW"
        base, ext = os.path.splitext(self.caminho)
        if re.fullmatch(r"\.\d{3}", ext):                       # disco.001, disco.002 ...
            padrao = glob.escape(base) + ".[0-9][0-9][0-9]"
            self.segmentos = sorted(glob.glob(padrao))
        elif re.fullmatch(r"\.(aa|ab)", ext):                  # split -a 2: disco.aa, disco.ab
            self.segmentos = sorted(glob.glob(glob.escape(base) + ".[a-z][a-z]"))
        else:
            self.segmentos = [self.caminho]
        pos = 0
        for s in self.segmentos:
            n = os.path.getsize(s)
            self._raw.append((pos, pos + n, s, open(s, "rb")))
            pos += n
        self.tamanho = pos

    # ------------------------------------------------------------------ leitura
    def ler(self, offset, n):
        if offset >= self.tamanho or n <= 0:
            return b""
        n = min(n, self.tamanho - offset)
        if self._ewf is not None:
            try:
                self._ewf.seek(offset)
                return self._ewf.read(n)
            except OSError:
                return self._ler_em_pedacos(offset, n)
        partes = []
        for ini, fim, _, fh in self._raw:
            if offset >= fim or offset + n <= ini:
                continue
            a = max(offset, ini)
            b = min(offset + n, fim)
            fh.seek(a - ini)
            partes.append(fh.read(b - a))
        return b"".join(partes)

    def _ler_em_pedacos(self, offset, n):
        """Refaz a leitura em pedaços de 64 KiB; os ilegíveis viram zeros e entram em self.falhas."""
        partes, p, fim = [], offset, offset + n
        while p < fim:
            k = min(PEDACO - (p % PEDACO), fim - p)
            try:
                self._ewf.seek(p)
                b = self._ewf.read(k)
            except OSError:
                b = b"\x00" * k
                if self.falhas and self.falhas[-1][1] == p:
                    self.falhas[-1] = (self.falhas[-1][0], p + k)
                elif not any(a <= p < z for a, z in self.falhas[-4:]):
                    self.falhas.append((p, p + k))
            partes.append(b)
            p += k
        return b"".join(partes)

    def bytes_ilegiveis(self):
        return sum(b - a for a, b in self.falhas)

    def fechar(self):
        if self._ewf is not None:
            self._ewf.close()
        for *_, fh in self._raw:
            fh.close()

    def descricao(self):
        return "%s · %d segmento(s) · %s bytes" % (
            self.tipo, len(self.segmentos), format(self.tamanho, ",").replace(",", "."))
