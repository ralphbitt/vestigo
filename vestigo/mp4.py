# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Leitura estrutural de MP4/MOV sem decodificar: árvore de boxes, tabela de amostras do
vídeo (stsz/stco/stsc), parâmetros do codificador (avcC) e validação de amostras H.264.

"""
import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}
TOPO = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"udta", b"meta", b"pdin",
        b"moof", b"mfra", b"sidx", b"styp", b"pnot", b"PICT"}
NAL_VALIDOS = {1, 5, 6, 7, 8, 9, 10, 11, 12}
NAL_VALIDOS_HEVC = set(range(0, 10)) | set(range(16, 22)) | set(range(32, 41))
CODECS_HEVC = (b"hvc1", b"hev1")


def ler_boxes(ler, ini, fim):
    """Percorre boxes irmãos em [ini, fim). ler(off, n) -> bytes. Devolve [(tipo, off, tam, hdr)]."""
    out, p = [], ini
    while p + 8 <= fim:
        h = ler(p, 16)
        if len(h) < 8:
            break
        tam, tipo = struct.unpack(">I4s", h[:8])
        hdr = 8
        if tam == 1:
            if len(h) < 16:
                break
            tam, hdr = struct.unpack(">Q", h[8:16])[0], 16
        elif tam == 0:
            tam = fim - p
        if tam < hdr or p + tam > fim:
            break
        out.append((tipo, p, tam, hdr))
        p += tam
    return out


def cadeia_topo(ler, ini, limite):
    """Boxes de nível superior a partir de um 'ftyp'. Devolve (boxes, fim) ou (None, None)."""
    boxes, p = [], ini
    while p < limite:
        h = ler(p, 16)
        if len(h) < 8:
            break
        tam, tipo = struct.unpack(">I4s", h[:8])
        if tipo not in TOPO:
            break
        hdr = 8
        if tam == 1:
            tam, hdr = struct.unpack(">Q", h[8:16])[0], 16
        if tam < hdr or p + tam > limite:
            break
        boxes.append((tipo, p, tam, hdr))
        p += tam
        if {b"moov", b"mdat"} <= {b[0] for b in boxes} and tipo in (b"moov", b"mdat"):
            # completo; continua só se o próximo também for box válido
            prox = ler(p, 8)
            if len(prox) < 8 or prox[4:8] not in TOPO:
                break
    tipos = {b[0] for b in boxes}
    if boxes and boxes[0][0] == b"ftyp" and {b"moov", b"mdat"} <= tipos:
        return boxes, p
    return None, None


class Faixa:
    """Informação de uma trilha (vídeo ou áudio) tirada do moov."""

    def __init__(self):
        self.handler = b""
        self.largura = self.altura = 0
        self.canais = 0
        self.asc = b""              # AudioSpecificConfig (AAC), do esds
        self.bitrate = 0
        self.timescale = 0
        self.duracao = 0
        self.tamanhos = []
        self.offsets = []           # offset de cada amostra, relativo ao início do arquivo
        self.sps, self.pps = [], []
        self.nal_len = 4
        self.codec = b""

    @property
    def fps(self):
        if self.duracao and self.timescale and self.tamanhos:
            return len(self.tamanhos) * self.timescale / self.duracao
        return 0.0

    @property
    def segundos(self):
        return self.duracao / self.timescale if self.timescale else 0.0


def _filhos(moov, a, b):
    return ler_boxes(lambda o, n: moov[o:o + n], a, b)


def _descritor(b, p):
    """Lê tag e tamanho (varint de até 4 bytes) de um descritor MPEG-4."""
    tag, ln = b[p], 0
    p += 1
    for _ in range(4):
        c = b[p]
        p += 1
        ln = (ln << 7) | (c & 0x7F)
        if not c & 0x80:
            break
    return tag, ln, p


def _esds(corpo, f):
    """esds -> bitrate médio e AudioSpecificConfig."""
    try:
        tag, ln, p = _descritor(corpo, 4)
        if tag != 3:
            return
        p += 2
        fl = corpo[p]
        p += 1
        if fl & 0x80:
            p += 2
        if fl & 0x40:
            p += 1 + corpo[p]
        if fl & 0x20:
            p += 2
        tag, ln, p = _descritor(corpo, p)
        if tag != 4:
            return
        f.bitrate = struct.unpack(">I", corpo[p + 9:p + 13])[0]
        tag, ln, q = _descritor(corpo, p + 13)
        if tag == 5:
            f.asc = corpo[q:q + ln]
    except (IndexError, struct.error):
        pass


def trilha_video(moov):
    """moov: bytes do box moov inteiro. Devolve Faixa da primeira trilha de vídeo ou None."""
    return next((f for f in trilhas(moov) if f.handler == b"vide"), None)


def trilha_audio(moov):
    return next((f for f in trilhas(moov) if f.handler == b"soun"), None)


def trilhas(moov):
    """Todas as trilhas do moov (vídeo e áudio), com tabela de amostras."""
    out = []
    for tipo, o, t, h in _filhos(moov, 8, len(moov)):
        if tipo != b"trak":
            continue
        f = Faixa()
        pilha = [(o + h, o + t)]
        stsc, stco = [], []
        while pilha:
            a, b = pilha.pop()
            for tp, oo, tt, hh in _filhos(moov, a, b):
                corpo = moov[oo + hh:oo + tt]
                if tp in (b"mdia", b"minf", b"stbl", b"trak"):
                    pilha.append((oo + hh, oo + tt))
                elif tp == b"hdlr":                 # QuickTime tem um 2º hdlr ('alis') no minf
                    if corpo[8:12] in (b"vide", b"soun"):
                        f.handler = corpo[8:12]
                elif tp == b"mdhd":
                    if corpo[0] == 1:
                        f.timescale, f.duracao = struct.unpack(">IQ", corpo[20:32])
                    else:
                        f.timescale, f.duracao = struct.unpack(">II", corpo[12:20])
                elif tp == b"stsd" and len(corpo) > 16:
                    ent = corpo[8:]
                    f.codec = ent[4:8]
                    if f.codec in (b"mp4a",):
                        f.canais = struct.unpack(">H", ent[24:26])[0]
                        i = ent.find(b"esds")
                        if i > 0:
                            ln = struct.unpack(">I", ent[i - 4:i])[0]
                            _esds(ent[i + 4:i - 4 + ln], f)
                        continue
                    f.largura, f.altura = struct.unpack(">HH", ent[32:36])
                    i = ent.find(b"avcC")
                    if i > 0:
                        c = ent[i + 4:]
                        f.nal_len = (c[4] & 3) + 1
                        n_sps, p = c[5] & 0x1F, 6
                        for _ in range(n_sps):
                            ln = struct.unpack(">H", c[p:p + 2])[0]
                            f.sps.append(c[p + 2:p + 2 + ln]); p += 2 + ln
                        n_pps = c[p]; p += 1
                        for _ in range(n_pps):
                            ln = struct.unpack(">H", c[p:p + 2])[0]
                            f.pps.append(c[p + 2:p + 2 + ln]); p += 2 + ln
                elif tp == b"stsz":
                    tam_fixo, n = struct.unpack(">II", corpo[4:12])
                    f.tamanhos = ([tam_fixo] * n if tam_fixo else
                                  list(struct.unpack(">%dI" % n, corpo[12:12 + 4 * n])))
                elif tp == b"stsc":
                    n = struct.unpack(">I", corpo[4:8])[0]
                    stsc = [struct.unpack(">III", corpo[8 + 12 * k:20 + 12 * k]) for k in range(n)]
                elif tp == b"stco":
                    n = struct.unpack(">I", corpo[4:8])[0]
                    stco = list(struct.unpack(">%dI" % n, corpo[8:8 + 4 * n]))
                elif tp == b"co64":
                    n = struct.unpack(">I", corpo[4:8])[0]
                    stco = list(struct.unpack(">%dQ" % n, corpo[8:8 + 8 * n]))
        if not f.handler:
            continue
        # offset de cada amostra a partir de stco + stsc + stsz
        k = 0
        for ci, base in enumerate(stco):
            por_chunk = 0
            for j, (primeiro, spc, _) in enumerate(stsc):
                prox = stsc[j + 1][0] if j + 1 < len(stsc) else 1 << 62
                if primeiro <= ci + 1 < prox:
                    por_chunk = spc
                    break
            o_ = base
            for _ in range(por_chunk):
                if k >= len(f.tamanhos):
                    break
                f.offsets.append(o_)
                o_ += f.tamanhos[k]
                k += 1
        out.append(f)
    return out


class _Bits:
    def __init__(self, nal):
        rbsp = bytearray()                       # remove bytes de prevenção de emulação 00 00 03
        z = 0
        for b in nal[1:]:
            if z >= 2 and b == 3:
                z = 0
                continue
            rbsp.append(b)
            z = z + 1 if b == 0 else 0
        self.d, self.p = bytes(rbsp), 0

    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.d[self.p >> 3] if (self.p >> 3) < len(self.d) else 0
            v = (v << 1) | ((byte >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0 and z < 32:
            z += 1
        return (1 << z) - 1 + self.u(z)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def sps_info(nal):
    """Largura, altura e fps (do VUI, se presente) de um NAL SPS H.264."""
    try:
        b = _Bits(nal)
        prof = b.u(8); b.u(16); b.ue()
        if prof in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            if b.ue() == 3:
                b.u(1)
            b.ue(); b.ue(); b.u(1)
            if b.u(1):
                for i in range(8):
                    if b.u(1):
                        ultimo = prox = 8
                        for _ in range(16 if i < 6 else 64):
                            if prox:
                                prox = (ultimo + b.se() + 256) % 256
                            ultimo = prox or ultimo
        b.ue()
        poc = b.ue()
        if poc == 0:
            b.ue()
        elif poc == 1:
            b.u(1); b.se(); b.se()
            for _ in range(b.ue()):
                b.se()
        b.ue(); b.u(1)
        w, h = b.ue() + 1, b.ue() + 1
        frame_only = b.u(1)
        if not frame_only:
            b.u(1)
        b.u(1)
        crop = [0, 0, 0, 0]
        if b.u(1):
            crop = [b.ue() for _ in range(4)]
        largura = w * 16 - 2 * (crop[0] + crop[1])
        altura = (2 - frame_only) * h * 16 - 2 * (2 - frame_only) * (crop[2] + crop[3])
        fps = 0.0
        if b.u(1):                                   # vui_parameters_present
            if b.u(1) and b.u(8) == 255:
                b.u(32)
            if b.u(1):
                b.u(1)
            if b.u(1):
                b.u(4)
                if b.u(1):
                    b.u(24)
            if b.u(1):
                b.ue(); b.ue()
            if b.u(1):                               # timing_info_present
                tick, escala = b.u(32), b.u(32)
                if tick:
                    fps = escala / (2.0 * tick)
        return largura, altura, fps
    except IndexError:
        return 0, 0, 0.0


def nal_ok(dado, p, hevc=False):
    """Cabeçalho de NAL plausível: H.264 (1 byte) ou HEVC (2 bytes, tipos 0–40)."""
    h = dado[p]
    if h & 0x80:
        return False
    if not hevc:
        return (h & 0x1F) in NAL_VALIDOS
    if p + 1 >= len(dado):
        return True
    return ((h >> 1) & 0x3F) in NAL_VALIDOS_HEVC and (dado[p + 1] & 7) != 0   # temporal_id_plus1


def amostra_valida(dado, tam, nal_len=4, hevc=False):
    """A amostra começa com uma cadeia coerente de NALs em formato AVCC/HVCC?"""
    p, lidos = 0, 0
    while p + nal_len < len(dado) and p < tam:
        ln = int.from_bytes(dado[p:p + nal_len], "big")
        if ln == 0 or p + nal_len + ln > tam:
            return False
        if not nal_ok(dado, p + nal_len, hevc):
            return False
        lidos += 1
        p += nal_len + ln
        if lidos >= 2:
            return True
    # só se leu o começo da amostra: basta a 1ª NAL ser válida e caber nela (no HEVC o quadro
    # costuma começar por uma NAL grande, e a 2ª fica além dos bytes lidos)
    return lidos >= 1 and p <= tam


def cadeia_avcc(ler, o, limite, max_nal=8 << 20):
    """Segue NALs AVCC a partir de o. Devolve [(off_nal, len)] até a cadeia romper."""
    out = []
    while o + 5 <= limite:
        h = ler(o, 5)
        if len(h) < 5:
            break
        ln = int.from_bytes(h[:4], "big")
        t = h[4]
        if ln == 0 or ln > max_nal or t & 0x80 or (t & 0x1F) not in NAL_VALIDOS or o + 4 + ln > limite:
            break
        out.append((o + 4, ln))
        o += 4 + ln
    return out
