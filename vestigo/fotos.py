# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validação de fotos JPEG recuperadas (.jpg, .insp da Insta360 e afins).

O .insp é um JPEG comum — as duas lentes lado a lado — seguido de um trailer de metadados da
câmera depois do fim da imagem (EOI). A validação tem duas partes:
  1. estrutura: percorre os marcadores do JPEG até o EOI (SOI, segmentos, SOF, SOS, dados);
  2. decodificação: o decodificador MJPEG do FFmpeg (PyAV) abre a imagem, e os erros que ele
     registrar (dados entrópicos corrompidos, fim prematuro) ficam no estado da foto.
"""
import re
import struct

EXT_FOTO = (".insp", ".jpg", ".jpeg", ".jpe", ".jfif")


LACUNA = re.compile(rb"\x00{4096,}|\xff{4096,}")


def estrutura_jpeg(dado):
    """-> dict(ok, largura, altura, fim_eoi, motivo).

    Além de achar o EOI, recusa o que o decodificador aceitaria calado: medido numa JPEG
    partida em dois com 20 MB de zeros no meio, o MJPEG do FFmpeg decodificou "sem erro".
    Por isso: 4 KiB de bytes iguais dentro dos dados comprimidos é lacuna (fragmento ou bloco
    apagado), e mais de 3 bytes por pixel é maior que a própria imagem sem compressão."""
    r = estrutura_jpeg_bruta(dado)
    if not r["ok"]:
        return r
    m = LACUNA.search(dado, r["sos"], r["fim_eoi"])        # só nos dados comprimidos
    if m:
        r["ok"] = False
        r["motivo"] = "lacuna de %d bytes iguais (0x%02X) dentro da imagem, no byte %d: fragmentada ou " \
                      "parcialmente apagada" % (m.end() - m.start(), dado[m.start()], m.start())
    elif r["largura"] and r["altura"] and r["fim_eoi"] > r["largura"] * r["altura"] * 3 + (1 << 20):
        r["ok"] = False
        r["motivo"] = "tamanho incompatível com %sx%s: há outro conteúdo entre o início e o fim" % (
            r["largura"], r["altura"])
    return r


def estrutura_jpeg_bruta(dado):
    """Só a estrutura de marcadores: -> dict(ok, largura, altura, fim_eoi, motivo)."""
    r = {"ok": False, "largura": None, "altura": None, "fim_eoi": None, "motivo": None, "sos": 0}
    if dado[:3] != b"\xff\xd8\xff":
        r["motivo"] = "não começa com SOI (FF D8 FF)"
        return r
    p = 2
    while p + 4 <= len(dado):
        if dado[p] != 0xFF:
            r["motivo"] = "marcador inválido em %d" % p
            return r
        m = dado[p + 1]
        if m == 0xFF:
            p += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        ln = struct.unpack(">H", dado[p + 2:p + 4])[0]
        if m in (0xC0, 0xC1, 0xC2, 0xC3) and p + 9 <= len(dado):
            r["altura"], r["largura"] = struct.unpack(">HH", dado[p + 5:p + 9])
        if m == 0xDA:                              # SOS: dados entrópicos até o próximo marcador
            q = p + 2 + ln
            r["sos"] = r["sos"] or q
            while True:
                q = dado.find(b"\xff", q)
                if q < 0 or q + 1 >= len(dado):
                    r["motivo"] = "dados da imagem terminam sem EOI (FF D9)"
                    return r
                n = dado[q + 1]
                if n == 0x00 or 0xD0 <= n <= 0xD7 or n == 0xFF:
                    q += 2 if n != 0xFF else 1
                    continue
                if n == 0xD9:
                    r["ok"], r["fim_eoi"] = True, q + 2
                    return r
                p = q                               # outro SOS (JPEG progressivo) ou DHT
                break
            continue
        p += 2 + ln
    r["motivo"] = "estrutura termina antes do SOS/EOI"
    return r


def decodificar(caminho):
    """Abre a imagem no decodificador do FFmpeg. -> (decodificou, mensagens de erro)."""
    try:
        import av
    except ImportError:
        return None, []
    try:
        with av.logging.Capture() as logs:
            with av.open(caminho, format="jpeg_pipe") as c:
                quadros = sum(1 for _ in c.decode(video=0))
        erros = sorted({m for nivel, _, m in logs if nivel <= av.logging.ERROR})
        return quadros > 0, erros
    except av.error.FFmpegError as e:
        return False, [str(e)]
