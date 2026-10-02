# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recuperação de áudio AAC de fluxos sem índice.

Num MP4, vídeo e áudio se alternam em blocos (chunks) dentro do mdat. Remontado o vídeo, as
lacunas entre seus trechos são os blocos de áudio: AAC cru, sem palavra de sincronismo e sem
a tabela de tamanhos (stsz). Para dividir cada bloco em quadros usa-se o fato de o áudio ser
de taxa constante (CBR com reservatório de bits): quadros isolados variam, mas o início do
k-ésimo quadro fica perto da posição k × tamanho médio. Uma programação dinâmica acha a
divisão em que
    (a) todo início fica na grade do CBR (a menos de DESVIO quadro),
    (b) todo início tem cabeçalho AAC-LC plausível (elemento, tag, ics_info),
    (c) todo quadro decodifica no decodificador AAC do FFmpeg (PyAV),
    (d) a soma fecha exatamente no fim do bloco,
e, entre as que restarem, a de menos quadros fora dos tamanhos típicos. O perfil vem de outro
vídeo do dispositivo ou é estimado pela distância mais frequente entre inícios válidos.
Validado em 922 blocos de áudio de vídeos reais com índice: a divisão feita sem o índice
coincidiu com o índice original em 100% dos blocos.
Blocos sem solução viram silêncio, para o áudio não perder a sincronia com o vídeo.

Áudio VBR (tamanho variável quadro a quadro) não tem solução única sem o índice: nesse caso
o programa declara que não recuperou, em vez de adivinhar.

"""
import bisect
import collections
import os
import subprocess

import numpy as np

TAXAS = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
DESVIO = 0.6            # afastamento máximo da grade do CBR, em frações de quadro
SEM_JANELA = 0x08000000 if os.name == "nt" else 0


class Perfil:
    """Perfil de um AAC CBR: tamanhos típicos de quadro, taxa, canais."""

    def __init__(self, tamanhos, media, taxa=48000, canais=2, aot=2, origem=""):
        self.tamanhos = tuple(sorted(tamanhos))
        self.media = media
        self.taxa, self.canais, self.aot = taxa, canais, aot
        self.origem = origem

    @property
    def elemento(self):
        return 1 if self.canais == 2 else 0          # CPE para estéreo, SCE para mono

    @property
    def sf_index(self):
        return TAXAS.index(self.taxa) if self.taxa in TAXAS else 3

    def chave(self):
        return (self.tamanhos, self.taxa, self.canais)

    def __repr__(self):
        return "AAC %d Hz %s, quadros %s B (%s)" % (
            self.taxa, "estéreo" if self.canais == 2 else "%d canal(is)" % self.canais,
            "/".join(map(str, self.tamanhos)), self.origem)


def perfil_de_trilha(tr, origem=""):
    """Faixa de áudio (mp4.Faixa) -> Perfil, se for CBR; None se VBR.

    CBR com reservatório de bits: quadros isolados variam (em gravações reais de média 846 bytes, de 599 a 1.094),
    mas se compensam, e o início de cada quadro nunca se afasta mais de meio quadro da grade
    k × tamanho médio. Em VBR esse desvio acumulado cresce sem limite."""
    if not tr or tr.codec != b"mp4a" or len(tr.tamanhos) < 20:
        return None
    t = np.asarray(tr.tamanhos, np.float64)
    media = float(t.mean())
    desvio = np.abs(np.cumsum(t) - media * np.arange(1, len(t) + 1)).max()
    if desvio > media * 0.5:
        return None                                   # VBR
    c = collections.Counter(tr.tamanhos)
    tops = [x for x, _ in c.most_common(2)]
    if len(tops) == 2 and abs(tops[0] - tops[1]) > 2:
        tops = tops[:1]
    taxa, canais, aot = tr.timescale or 48000, tr.canais or 2, 2
    if len(tr.asc) >= 2:
        aot = tr.asc[0] >> 3
        sfi = ((tr.asc[0] & 7) << 1) | (tr.asc[1] >> 7)
        taxa = TAXAS[sfi] if sfi < len(TAXAS) else taxa
        canais = (tr.asc[1] >> 3) & 0xF or canais
    return Perfil(tops, media, taxa, canais, aot, origem)


def _bits(buf, q, ini, n):
    """n bits a partir do bit 'ini' (0 = MSB) do byte q."""
    v = int.from_bytes(buf[q:q + 4].ljust(4, b"\0"), "big")
    return (v >> (32 - ini - n)) & ((1 << n) - 1)


def _ics_info_ok(buf, q, bit):
    """ics_info do AAC-LC: bit reservado 0, max_sfb dentro do limite, sem predição."""
    if _bits(buf, q, bit, 1):                          # ics_reserved_bit
        return False, 0
    seq = _bits(buf, q, bit + 1, 2)                    # window_sequence
    if seq == 2:                                       # EIGHT_SHORT_SEQUENCE
        return _bits(buf, q, bit + 4, 4) <= 14, bit + 4 + 4 + 7
    if _bits(buf, q, bit + 4, 6) > 49:                 # max_sfb (janela longa)
        return False, 0
    return _bits(buf, q, bit + 10, 1) == 0, bit + 11   # predictor_data_present


def inicio_valido(buf, q, el):
    """O quadro que começa em q tem o cabeçalho de um elemento AAC-LC plausível?"""
    b0 = buf[q]
    if b0 >> 5 != el or (b0 >> 1) & 0x0F != 0:        # id_syn_ele e element_instance_tag = 0
        return False
    if el == 0:                                        # SCE: global_gain (8 bits) e ics_info
        return _ics_info_ok(buf, q, 7 + 8)[0]
    if not b0 & 1:                                     # CPE sem common_window: ics_info vem depois
        return True
    ok, fim = _ics_info_ok(buf, q, 8)
    if not ok:
        return False
    return _bits(buf, q + fim // 8, fim % 8, 2) != 3   # ms_mask_present = 3 é reservado


def inicios_validos(buf, el):
    a = np.frombuffer(buf, np.uint8)
    pre = np.flatnonzero((a & 0xFE) == (el << 5)).tolist()        # id + instance_tag = 0
    return [q for q in pre if q + 4 <= len(buf) and inicio_valido(buf, q, el)]


class Decodificador:
    """O decodificador AAC do FFmpeg (via PyAV) como juiz de cada quadro candidato.

    Medido em quadros reais: aceita 100% dos verdadeiros e recusa 100% dos curtos demais
    e dos de início deslocado. Aceita os longos demais (ignora a sobra), mas aí o quadro
    seguinte começa fora do lugar e é recusado; por isso a cadeia inteira fica determinada."""

    def __init__(self, perfil):
        import av
        self._erro = av.error.FFmpegError
        self._pacote = av.Packet
        self.ctx = av.CodecContext.create("aac", "r")
        asc = (perfil.aot << 11) | (perfil.sf_index << 7) | (perfil.canais << 3)
        self.ctx.extradata = asc.to_bytes(2, "big")
        self.ctx.open()

    def ok(self, quadro):
        try:
            self.ctx.decode(self._pacote(quadro))
            return True
        except self._erro:
            return False


def decodificador(perfil):
    try:
        return Decodificador(perfil)
    except Exception:                                  # PyAV ausente: só as regras estruturais
        return None


def dividir(buf, perfil, dec=None):
    """Divide um bloco de áudio em quadros. Devolve (tamanhos, n_fora_do_padrão) ou None.

    Regras: o k-ésimo quadro começa a menos de DESVIO × média da posição k × média (grade do
    CBR); todo início tem cabeçalho AAC válido; se houver decodificador, todo quadro tem de
    decodificar; a soma fecha no fim do bloco. Entre as divisões possíveis, a que usa menos
    quadros fora dos tamanhos típicos."""
    L = len(buf)
    m = perfil.media
    if L < m * 0.5:
        return None
    inicios = inicios_validos(buf, perfil.elemento)
    if not inicios or inicios[0] != 0:
        return None
    cache = {}

    def decodifica(p, q):
        if dec is None:
            return True
        if (p, q) not in cache:
            cache[(p, q)] = dec.ok(buf[p:q])
        return cache[(p, q)]
    D, lo, hi = DESVIO * m, int(0.6 * m), int(1.4 * m)
    padrao = set(perfil.tamanhos)
    n0 = max(1, round(L / m))
    melhor = None
    for n in (n0, n0 - 1, n0 + 1):
        if n < 1 or abs(L - n * m) > D:
            continue
        camadas = [{0: (0, None)}]            # posição -> (custo, posição anterior)
        for k in range(1, n + 1):
            prox = {}
            alvo = k * m
            for p, (custo, _) in camadas[-1].items():
                a, b = max(p + lo, alvo - D), min(p + hi, alvo + D)
                if k == n:
                    cands = [L] if a <= L <= b else []
                else:
                    i = bisect.bisect_left(inicios, a)
                    j = bisect.bisect_right(inicios, b)
                    cands = inicios[i:j]
                for q in cands:
                    if not decodifica(p, q):
                        continue
                    c = custo + (0 if q - p in padrao else 1)
                    if q not in prox or prox[q][0] > c:
                        prox[q] = (c, p)
            if not prox:
                break
            camadas.append(prox)
        else:
            if L in camadas[-1] and (melhor is None or camadas[-1][L][0] < melhor[1]):
                tams, p = [], L
                for c in range(len(camadas) - 1, 0, -1):
                    ant = camadas[c][p][1]
                    tams.append(p - ant)
                    p = ant
                melhor = (list(reversed(tams)), camadas[-1][L][0])
    return melhor


def estimar_perfil(blocos, canais_teste=(2, 1)):
    """Sem doador: o tamanho típico de quadro é a distância mais frequente entre inícios válidos."""
    amostra = [b for b in blocos if len(b) > 2000][:20]
    if len(amostra) < 3:
        return None
    melhor, melhor_ok = None, 0
    for canais in canais_teste:
        el = 1 if canais == 2 else 0
        dist = collections.Counter()
        for b in amostra:
            ini = inicios_validos(b, el)
            for i, p in enumerate(ini):
                for q in ini[i + 1:i + 40]:
                    if 96 <= q - p <= 2048:
                        dist[q - p] += 1
        if not dist:
            continue
        s = dist.most_common(1)[0][0]
        vizinho = max((s - 1, s + 1), key=lambda x: dist[x])
        par = sorted((s, vizinho)) if dist[vizinho] > dist[s] * 0.2 else [s]
        media = sum(x * dist[x] for x in par) / sum(dist[x] for x in par)
        p = Perfil(par, media, canais=canais, origem="estimado pelos próprios blocos")
        ok = sum(1 for b in amostra if dividir(b, p))
        if ok > melhor_ok:
            melhor, melhor_ok = p, ok
    return melhor if melhor_ok >= len(amostra) * 0.4 else None


def cabecalho_adts(n, perfil):
    L = n + 7
    return bytes((0xFF, 0xF1,
                  ((perfil.aot - 1) << 6) | (perfil.sf_index << 2) | (perfil.canais >> 2),
                  ((perfil.canais & 3) << 6) | ((L >> 11) & 0x03),
                  (L >> 3) & 0xFF, ((L & 0x07) << 5) | 0x1F, 0xFC))


def quadro_silencio(ffmpeg, perfil, tmp):
    """Um quadro AAC de silêncio no mesmo perfil, gerado pelo FFmpeg (sem cabeçalho ADTS)."""
    arq = os.path.join(tmp, "silencio.aac")
    kbps = int(perfil.media * 8 * perfil.taxa / 1024 / 1000)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=%d:cl=%s" % (perfil.taxa, "stereo" if perfil.canais == 2 else "mono"),
                    "-t", "1", "-c:a", "aac", "-b:a", "%dk" % max(kbps, 32), "-f", "adts", arq],
                   capture_output=True, creationflags=SEM_JANELA)
    if not os.path.isfile(arq):
        return None
    b = open(arq, "rb").read()
    os.remove(arq)
    quadros, p = [], 0
    while p + 7 <= len(b) and b[p] == 0xFF:
        L = ((b[p + 3] & 3) << 11) | (b[p + 4] << 3) | (b[p + 5] >> 5)
        quadros.append(b[p + 7:p + L])
        p += L
    return quadros[len(quadros) // 2] if quadros else None


def escrever_adts(blocos, perfil, destino, silencio=None, log=print):
    """Divide todos os blocos e grava um .aac (ADTS). Devolve estatísticas."""
    dec = decodificador(perfil)
    ok_bytes = tot_bytes = quadros = sil = fora_total = falhos = descartados = 0
    with open(destino, "wb") as out:
        for buf in blocos:
            r = dividir(buf, perfil, dec)
            if not r and (len(buf) < 4 or not inicio_valido(buf, 0, perfil.elemento)):
                descartados += 1          # não parece áudio (resto de vídeo): não vira silêncio
                continue
            tot_bytes += len(buf)
            if not r:
                falhos += 1
                n = max(1, round(len(buf) / perfil.media))
                if silencio:
                    for _ in range(n):
                        out.write(cabecalho_adts(len(silencio), perfil) + silencio)
                    sil += n
                    quadros += n
                continue
            tams, fora = r
            fora_total += fora
            ok_bytes += len(buf)
            p = 0
            for t in tams:
                out.write(cabecalho_adts(t, perfil) + buf[p:p + t])
                p += t
                quadros += 1
    return {"blocos": len(blocos), "blocos_falhos": falhos, "blocos_descartados": descartados,
            "validado_pelo_decodificador": dec is not None,
            "quadros": quadros,
            "quadros_silencio": sil, "quadros_fora_do_padrao": fora_total,
            "percentual": 100.0 * ok_bytes / tot_bytes if tot_bytes else 0.0}
