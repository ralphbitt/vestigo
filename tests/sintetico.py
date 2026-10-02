# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geradores de dados sintéticos para os testes: nenhum dado real é usado."""
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
