# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cartão de câmera sintético para demonstração e capturas de tela.

Monta um exFAT de 256 MiB com tudo o que o Vestigo sabe tratar, gerado na hora pelo FFmpeg:
vídeos e fotos vivos, apagados com a cadeia preservada (um deles fragmentado), apagados com o
conteúdo zerado e com clusters reaproveitados, um MP4 inteiro e uma foto fora do sistema de
arquivos (carving) e um fluxo H.264 sem índice (só o mdat de uma gravação interrompida).

    python -m ferramentas.demo PASTA
"""
import os
import subprocess
import sys
import tempfile

from tests.sintetico import exfat, jpeg
from vestigo.mp4 import cadeia_topo
from vestigo.recuperacao import achar_ffmpeg

KiB, MiB, CL = 1 << 10, 1 << 20, 32 << 10
FFMPEG, _ = achar_ffmpeg()

FONTES = ["testsrc2", "smptehdbars", "mandelbrot", "life=s=1280x720:mold=10:r=30:ratio=0.1",
          "rgbtestsrc", "cellauto=s=1280x720:rule=110:r=30"]


def video(fonte, segundos, tmp, nome):
    destino = os.path.join(tmp, nome)
    src = fonte if "=" in fonte else fonte + "=s=1280x720:r=30"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src + ",noise=alls=12:allf=t",
                    "-f", "lavfi", "-i", "sine=f=440:d=%d" % segundos, "-t", str(segundos),
                    "-c:v", "libx264", "-preset", "veryfast", "-b:v", "8M", "-maxrate", "8M", "-bufsize", "8M", "-pix_fmt", "yuv420p",
                    "-x264-params", "aud=1", "-c:a", "aac", "-b:a", "128k", "-shortest",
                    "-f", "mp4", destino], check=True)
    return open(destino, "rb").read()


def mdat(dado):
    """Só o conteúdo do mdat: o que sobra de uma gravação interrompida antes do moov."""
    boxes, _ = cadeia_topo(lambda o, n: dado[o:o + n], 0, len(dado))
    for tipo, ini, tam, cab in boxes:
        if tipo == b"mdat":
            return dado[ini + cab:ini + tam]
    raise ValueError("sem mdat")


def faixa(c, dado):
    return list(range(c, c + -(-len(dado) // CL)))


def montar(pasta):
    os.makedirs(pasta, exist_ok=True)
    duracoes = (20, 16, 18, 14, 12, 15)
    with tempfile.TemporaryDirectory() as tmp:
        v = [video(f, d, tmp, "v%d.mp4" % i) for i, (f, d) in enumerate(zip(FONTES, duracoes))]
        fotos = [jpeg(4000, 3000, semente=s, qualidade=3) for s in (1, 2, 3, 4, 5)]
    arqs, c = [{"nome": "DCIM", "pasta": True}], 8

    def add(nome, dado, clusters=None, **kw):
        nonlocal c
        cl = clusters or faixa(c, dado)
        c = max(c, cl[-1] + 1)
        arqs.append(dict(nome=nome, dados=dado, clusters=cl, pasta="DCIM", **kw))
        return cl

    add("VID_0001.MP4", v[0], data=(2026, 5, 7, 9, 12, 4))
    add("IMG_0001.JPG", fotos[0], data=(2026, 5, 7, 9, 14, 30))
    # apagado e fragmentado: as duas metades separadas por uma gravação que continua viva
    meio = (len(v[1]) // CL // 2) * CL
    a = faixa(c, v[1][:meio])
    c = a[-1] + 1
    add("VID_0003.MP4", v[2], data=(2026, 5, 7, 9, 30, 0))
    b = faixa(c, v[1][meio:])
    c = b[-1] + 1
    add("VID_0002.MP4", v[1], clusters=a + b, apagado=True, data=(2026, 5, 7, 9, 20, 10))
    add("IMG_0002.JPG", fotos[1], apagado=True, data=(2026, 5, 7, 9, 41, 2))
    add("VID_0004.MP4", v[3], apagado=True, zerado=True, data=(2026, 5, 7, 10, 2, 0))
    # apagado e depois sobrescrito por fotos novas: clusters reaproveitados
    reuso = add("VID_0005.MP4", v[4], apagado=True, data=(2026, 5, 7, 10, 10, 0))
    p = 0
    for k, (nome, foto) in enumerate((("IMG_0003.JPG", fotos[2]), ("IMG_0004.JPG", fotos[3]))):
        n = -(-len(foto) // CL)
        arqs.append(dict(nome=nome, dados=foto, clusters=reuso[p:p + n], pasta="DCIM",
                         data=(2026, 5, 7, 10, 30 + k, 0)))
        p += n
    livre = (c * CL) + 2 * MiB
    soltos = [(livre + 24 * MiB, v[5]), (livre + 48 * MiB, fotos[4]), (livre + 60 * MiB, mdat(v[0])[:14 * MiB])]
    return exfat(os.path.join(pasta, "cartao_demo.dd"), arqs, soltos=soltos)


if __name__ == "__main__":
    print(montar(sys.argv[1] if len(sys.argv) > 1 else "demo"))
