# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geradores de dados sintéticos para os testes: nenhum dado real é usado."""
import struct

import av
import numpy as np


def jpeg(largura, altura, semente=0, qualidade=4):
    """Uma JPEG real, codificada pelo MJPEG do FFmpeg, com imagem determinística."""
    rng = np.random.default_rng(semente)
    y, x = np.mgrid[0:altura, 0:largura]
    img = np.stack([(x * 255 // largura), (y * 255 // altura), ((x + y) % 256)], axis=-1)
    img = (img + rng.integers(0, 24, img.shape)).clip(0, 255).astype(np.uint8)
    quadro = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuvj420p")
    cod = av.CodecContext.create("mjpeg", "w")
    cod.width, cod.height, cod.pix_fmt = largura, altura, "yuvj420p"
    cod.options = {"qscale": str(qualidade)}
    cod.flags |= av.codec.context.Flags.qscale
    pacotes = cod.encode(quadro) + cod.encode(None)
    return bytes(pacotes[0])


def imagem_dd(caminho, tamanho=64 << 20, pecas=()):
    """Grava uma imagem dd zerada com as peças [(offset, bytes)] nas posições dadas."""
    img = bytearray(tamanho)
    for off, dado in pecas:
        img[off:off + len(dado)] = dado
    with open(caminho, "wb") as f:
        f.write(img)
    return caminho


def _dos(ano, mes, dia, h, mi, s):
    return ((ano - 1980) << 25) | (mes << 21) | (dia << 16) | (h << 11) | (mi << 5) | (s // 2)


def exfat(caminho, arquivos, tamanho=256 << 20, cluster=32 << 10, soltos=()):
    """Grava um cartão exFAT (superfloppy) sintético.

    arquivos: dicts com  nome, dados, clusters (lista explícita, para fragmentar ou sobrepor),
    pasta ("" = raiz), apagado (entrada 0x05 e bitmap livre, cadeia preservada na FAT),
    zerado (conteúdo apagado fisicamente, como faz o cartão SD) e data (ano, mês, dia, h, min, s).
    Pastas também entram como dicts com  pasta=True.  soltos: [(offset, bytes)] fora do sistema de
    arquivos, para o carving e a busca de fluxos. Clusters 2–5 são do bitmap, maiúsculas e raiz.
    """
    bps, spc = 512, cluster // 512
    heap_s = 2048
    n = (tamanho // bps - heap_s) // spc
    img = bytearray(tamanho)
    fat = np.zeros(n + 2, dtype="<u4")
    fat[0], fat[1] = 0xFFFFFFF8, 0xFFFFFFFF
    bitmap = np.zeros(n, dtype=bool)

    def off(c):
        return heap_s * bps + (c - 2) * cluster

    def gravar(clusters, dado, alocar=True):
        for k, c in enumerate(clusters):
            pedaco = dado[k * cluster:(k + 1) * cluster]
            img[off(c):off(c) + len(pedaco)] = pedaco
            fat[c] = clusters[k + 1] if k + 1 < len(clusters) else 0xFFFFFFFF
            if alocar:
                bitmap[c - 2] = True

    def entradas(nome, attr, primeiro, tam, apagado, data):
        nomeu = nome.encode("utf-16-le")
        partes = [nomeu[i:i + 30] for i in range(0, len(nomeu), 30)]
        ts = _dos(*data)
        fe = struct.pack("<BBHHHIIIBBBB", 0x05 if apagado else 0x85, 1 + len(partes), 0, attr, 0,
                         ts, ts, ts, 0, 0, 0, 0).ljust(32, b"\0")
        st = struct.pack("<BBBBHHQIIQ", 0x40 if apagado else 0xC0, 1, 0, len(nome), 0, 0, tam, 0,
                         primeiro, tam)
        nm = [struct.pack("<BB", 0x41 if apagado else 0xC1, 0) + p.ljust(30, b"\0") for p in partes]
        return fe + st + b"".join(nm)

    dirs = {"": [struct.pack("<BB18xIQ", 0x81, 0, 2, -(-n // 8)),
                 struct.pack("<B3xI12xIQ", 0x82, 0, 3, 128)]}
    raiz_c, prox = 4, 5
    pastas = {}
    for a in arquivos:
        if a.get("pasta") is True:
            pastas[a["nome"]] = prox
            prox += 1
            dirs[a["nome"]] = []
    for a in arquivos:
        data = a.get("data", (2026, 5, 7, 10, 0, 0))
        if a.get("pasta") is True:
            dirs[""].append(entradas(a["nome"], 0x10, pastas[a["nome"]], cluster, False, data))
            continue
        cl = a["clusters"]
        dado = bytes(len(a["dados"])) if a.get("zerado") else a["dados"]
        gravar(cl, dado, alocar=not a.get("apagado"))
        dirs[a.get("pasta", "")].append(entradas(a["nome"], 0x20, cl[0], len(a["dados"]),
                                                 a.get("apagado", False), data))
    for o, d in soltos:
        img[o:o + len(d)] = d
    gravar([2], np.packbits(bitmap, bitorder="little").tobytes())
    gravar([3], bytes(range(128)))
    gravar([raiz_c], b"".join(dirs[""]))
    for nome, c in pastas.items():
        gravar([c], b"".join(dirs[nome]))
    fat_s = 128
    img[fat_s * bps:fat_s * bps + len(fat) * 4] = fat.tobytes()
    usados = int(bitmap.sum())
    boot = bytearray(512)
    boot[0:3], boot[3:11] = b"\xeb\x76\x90", b"EXFAT   "
    struct.pack_into("<QQIIIIIIHH", boot, 64, 0, tamanho // bps, fat_s, 128, heap_s, n, raiz_c,
                     0x56455354, 0x0100, 0)
    boot[108], boot[109], boot[110], boot[111], boot[112] = 9, spc.bit_length() - 1, 1, 0x80, usados * 100 // n
    boot[510:512] = b"\x55\xaa"
    img[0:512] = boot
    with open(caminho, "wb") as f:
        f.write(img)
    return caminho
