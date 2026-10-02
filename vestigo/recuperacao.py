# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recuperação de vídeos a partir da imagem, em quatro frentes:

  1. vivos        vídeos presentes no sistema de arquivos e íntegros -> cópia lógica
  2. restaurados  vídeos com InitializedSize < RealSize -> leitura dos clusters reais no lugar
                  dos zeros que o NTFS devolve; corte no último quadro válido
  3. carving      MP4/MOV completos (ftyp + moov + mdat) fora de arquivos vivos
  4. fluxos       quadros H.264 sem índice (AVCC) em espaço livre ou não inicializado,
                  remontados e convertidos em MP4 pelo FFmpeg

Nada é gravado na imagem. Cada arquivo gerado recebe SHA-256 no relatório.
"""
import bisect
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess

import numpy as np

from . import audio, fotos, mp4
from .fotos import EXT_FOTO
from .ntfs import EXT_VIDEO

VERSAO = "0.1.0"


def achar_ffmpeg():
    """Ordem: variável VESTIGO_FFMPEG (pasta do bin), PATH, instalação do winget do usuário."""
    candidatas = []
    if os.environ.get("VESTIGO_FFMPEG"):
        candidatas.append(os.environ["VESTIGO_FFMPEG"])
    exe = shutil.which("ffmpeg")
    if exe and not candidatas:
        return exe, shutil.which("ffprobe")
    winget = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    candidatas += sorted(glob.glob(os.path.join(winget, "Gyan.FFmpeg*", "*", "bin")), reverse=True)
    for d in candidatas:
        if os.path.isfile(os.path.join(d, "ffmpeg.exe")):
            return os.path.join(d, "ffmpeg.exe"), os.path.join(d, "ffprobe.exe")
    return None, None


def sha256(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def mmss(seg):
    if seg is None:
        return "—"                                  # foto, ou vídeo sem duração legível
    seg = int(round(seg))
    return "%02dm%02ds" % (seg // 60, seg % 60) if seg < 3600 else "%dh%02dm%02ds" % (
        seg // 3600, seg % 3600 // 60, seg % 60)


def limpo(nome):
    nome = re.sub(r"[^\w\-]+", "-", os.path.splitext(nome)[0], flags=re.UNICODE).strip("-")
    return nome.upper()[:40] or "SEM-NOME"


def mapear(extents, a, b, base):
    """Faixa lógica [a, b) de um arquivo -> faixas físicas absolutas."""
    out = []
    for off, nb, l0 in extents:            # l0 = posição lógica (bytes) do início do trecho
        x, y = max(a, l0), min(b, l0 + nb)
        if x < y:
            out.append((base + off + (x - l0), base + off + (y - l0)))
    return out


class Recuperador:
    def __init__(self, img, vol, varredura, saida, log=print, ao_item=None, ao_etapa=None):
        self.img, self.vol, self.var = img, vol, varredura
        self.saida = saida
        self.log = log
        self.ao_item = ao_item or (lambda it: None)
        self.ao_etapa = ao_etapa or (lambda txt, frac: None)
        self.ffmpeg, self.ffprobe = achar_ffmpeg()
        self.itens = []
        self.excluir = []          # faixas físicas já explicadas (não viram "fluxo")
        self.doadores = []         # trilhas com SPS/PPS para fluxos sem parâmetros
        self.perfis_audio = []     # perfis AAC CBR dos vídeos conhecidos (doadores de áudio)
        self.nao_recuperados = []  # apagados localizados que não puderam sair, com o motivo
        self.parar = False
        self.audit = None

    # ------------------------------------------------------------------ infraestrutura
    def _auditar(self, evento, **kw):
        kw.update(ts=datetime.datetime.now().isoformat(timespec="seconds"), evento=evento)
        self.audit.write(json.dumps(kw, ensure_ascii=False) + "\n")
        self.audit.flush()

    def _pasta(self, nome, raiz="videos"):
        p = os.path.join(self.saida, raiz, nome)
        os.makedirs(p, exist_ok=True)
        return p

    def _ffprobe(self, arq):
        if not self.ffprobe:
            return {}
        r = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                            "-show_entries", "stream=width,height,nb_read_packets,r_frame_rate:"
                            "format=duration", "-of", "json", arq],
                           capture_output=True, text=True, creationflags=_SEM_JANELA)
        try:
            j = json.loads(r.stdout)
            s = (j.get("streams") or [{}])[0]
            return {"largura": s.get("width"), "altura": s.get("height"),
                    "quadros": int(s.get("nb_read_packets") or 0),
                    "duracao": float(j.get("format", {}).get("duration") or 0)}
        except (ValueError, KeyError):
            return {}

    def _registrar(self, it):
        it["sha256"] = sha256(it["arquivo"])
        it["tamanho"] = os.path.getsize(it["arquivo"])
        it["id"] = len(self.itens) + 1
        self.itens.append(it)
        self._auditar("arquivo_gerado", **{k: v for k, v in it.items() if k != "faixas"})
        self.log("  + %s" % os.path.basename(it["arquivo"]))
        self.ao_item(it)

    def _copiar_faixas(self, faixas, destino, limite=None):
        escrito = 0
        with open(destino, "wb") as out:
            for a, b in faixas:
                p = a
                while p < b and not self.parar:
                    n = min(b - p, 16 << 20)
                    if limite is not None:
                        n = min(n, limite - escrito)
                        if n <= 0:
                            return escrito
                    blk = self.img.ler(p, n)
                    out.write(blk)
                    escrito += len(blk)
                    p += n
        return escrito

    # ------------------------------------------------------------------ 1 e 2: MFT
    def _arquivos_video(self):
        if not self.vol:
            return []
        return [a for a in self.vol.arquivos.values()
                if a.em_uso and not a.dir and a.runs and a.video]

    def vivos_e_restaurados(self):
        cl, base = self.vol.cluster, self.vol.offset
        for a in self._arquivos_video():
            if self.parar:
                return
            ext = [(off, nb, vcn * cl) for off, nb, vcn in a.extents(cl)]
            fisico = mapear(ext, 0, a.real, base)
            nome = limpo(a.nome)
            lcn = "LCN%d-%d" % ((fisico[0][0] - base) // cl, (fisico[-1][1] - base - 1) // cl)
            if not a.danificado:
                self.ao_etapa("Exportando vídeo íntegro: %s" % a.nome, None)
                dest = os.path.join(self._pasta("1_vivos"), "V%02d_%s_%s.mp4" % (a.num, nome, lcn))
                self._copiar_faixas(fisico, dest, a.real)
                self.excluir += fisico
                tr = self._trilha_de_arquivo(dest)
                if tr:
                    self.doadores.append(tr)
                info = self._ffprobe(dest)
                self._registrar(dict(origem="vivo", nome_original=a.caminho, arquivo=dest,
                                     estado="íntegro no sistema de arquivos", faixas=fisico,
                                     criado=a.criado, **info))
                continue
            # ---- restaurado: os bytes além do InitializedSize vêm da mídia, não de zeros
            self.ao_etapa("Restaurando vídeo danificado: %s" % a.nome, None)
            pasta = self._pasta("2_restaurados")
            bruto = os.path.join(pasta, "R%02d_%s_%s_restaurado-bruto.mp4" % (a.num, nome, lcn))
            self._copiar_faixas(fisico, bruto, a.real)
            tr = self._trilha_de_arquivo(bruto)
            validos, seg, fim_valido = 0, 0.0, 0
            if tr and tr.offsets:
                validos = self._amostras_validas(bruto, tr)
                seg = tr.segundos * validos / max(len(tr.tamanhos), 1)
                fim_valido = tr.offsets[validos - 1] + tr.tamanhos[validos - 1] if validos else 0
                self.excluir += mapear(ext, 0, max(fim_valido, a.init), base)
                self.doadores.append(tr)
            self.log("  %s: %d de %d quadros válidos (%s de %s); InitializedSize parava em %.1f MiB"
                     % (a.nome, validos, len(tr.tamanhos) if tr else 0, mmss(seg),
                        mmss(tr.segundos if tr else 0), a.init / 2 ** 20))
            self._registrar(dict(origem="restaurado", nome_original=a.caminho, arquivo=bruto,
                                 estado="tamanho original; reproduz até %s" % mmss(seg), faixas=fisico,
                                 criado=a.criado, duracao=tr.segundos if tr else None,
                                 quadros_validos=validos,
                                 quadros=len(tr.tamanhos) if tr else None,
                                 largura=tr.largura if tr else None, altura=tr.altura if tr else None))
            if validos and self.ffmpeg:
                trecho = os.path.join(pasta, "R%02d_%s_%s_trecho-integro_%s.mp4"
                                      % (a.num, nome, lcn, mmss(seg)))
                subprocess.run([self.ffmpeg, "-v", "error", "-y", "-i", bruto, "-t", "%.3f" % seg,
                                "-c", "copy", trecho], capture_output=True, creationflags=_SEM_JANELA)
                if os.path.isfile(trecho):
                    info = self._ffprobe(trecho)
                    self._registrar(dict(origem="restaurado", nome_original=a.caminho, arquivo=trecho,
                                         estado="trecho íntegro, sem recodificação",
                                         faixas=mapear(ext, 0, fim_valido, base), criado=a.criado,
                                         **info))

    def _trilha_de_arquivo(self, caminho):
        with open(caminho, "rb") as f:
            def ler(o, n):
                f.seek(o)
                return f.read(n)
            tam = os.path.getsize(caminho)
            for tipo, o, t, h in mp4.ler_boxes(ler, 0, tam):
                if tipo == b"moov":
                    ts = mp4.trilhas(ler(o, t))
                    for tr in ts:                     # perfil de áudio: doador para os fluxos
                        p = audio.perfil_de_trilha(tr, os.path.basename(caminho)) if tr.handler == b"soun" else None
                        if p and p.chave() not in {x.chave() for x in self.perfis_audio}:
                            self.perfis_audio.append(p)
                    return next((tr for tr in ts if tr.handler == b"vide"), None)
        return None

    def _amostras_validas(self, caminho, tr):
        """Quantas amostras, a partir da primeira, estão onde o índice diz que estão."""
        ok = 0
        with open(caminho, "rb") as f:
            falhas = 0
            for k, (o, t) in enumerate(zip(tr.offsets, tr.tamanhos)):
                f.seek(o)
                if mp4.amostra_valida(f.read(min(t, 64)), t, tr.nal_len, tr.codec in mp4.CODECS_HEVC):
                    ok = k + 1
                    falhas = 0
                else:
                    falhas += 1
                    if falhas >= 3:          # tolera amostras isoladas estranhas (SEI longos)
                        break
        return ok

    # ------------------------------------------------------------------ 3: apagados pela cadeia
    def _ocupacao(self):
        """Quem ocupa cada cluster hoje (vivos) e qual apagado o usou por último."""
        if hasattr(self, "_dono"):
            return self._dono, self._usados
        dono, usados = {}, {}
        for a in self.vol.arquivos.values():
            if a.em_uso:
                for c, n in a.runs:
                    if c is not None:
                        for x in range(c, c + n):
                            dono[x] = a.nome
        apag = [a for a in self.vol.arquivos.values() if not a.em_uso and not a.dir and a.runs]
        for a in sorted(apag, key=lambda a: a.criado or ""):      # o mais recente fica por último
            for c, n in a.runs:
                if c is not None:
                    for x in range(c, c + n):
                        usados[x] = a.nome
        self._dono, self._usados = dono, usados
        return dono, usados

    def _candidatos(self, filtro):
        vistos, out = set(), []
        for a in self.vol.arquivos.values():
            if a.em_uso or a.dir or not a.runs or not a.real or not filtro(a):
                continue
            chave = (a.runs[0][0], a.real)
            if chave not in vistos:                 # a mesma entrada repetida no diretório
                vistos.add(chave)
                out.append(a)
        return sorted(out, key=lambda a: a.criado or "")

    def _triagem(self, a):
        """Situação física de um apagado. Devolve (fisico, obs, motivo_para_não_recuperar)."""
        v = self.vol
        cl, base = v.cluster, v.offset
        dono, usados = self._ocupacao()
        ext = [(off, nb, vcn * cl) for off, nb, vcn in a.extents(cl)]
        fisico = mapear(ext, 0, a.real, base)
        clus = [x for c, n in a.runs for x in range(c, c + n)][:-(-a.real // cl)]
        com_vivo = sum(1 for x in clus if x in dono)
        com_outro = sum(1 for x in clus if usados.get(x) != a.nome)
        amostra = [clus[int(i)] for i in np.linspace(0, len(clus) - 1, min(16, len(clus)))]
        zerados = sum(1 for x in amostra if not any(self.img.ler(base + x * cl, cl)))
        motivo = None
        if zerados == len(amostra):
            motivo = "conteúdo todo em zeros: blocos apagados fisicamente na mídia"
        elif com_vivo == len(clus):
            motivo = "todos os clusters reocupados por arquivos vivos"
        elif com_outro == len(clus):
            motivo = ("todos os clusters reaproveitados por gravação posterior (%s): o conteúdo "
                      "ali é de outro arquivo" % sorted({usados[x] for x in clus})[0])
        obs = []
        if not a.cadeia_completa:
            obs.append("cadeia da FAT/MFT termina antes do tamanho declarado")
        if com_vivo:
            obs.append("%d de %d clusters hoje pertencem a arquivos vivos" % (com_vivo, len(clus)))
        if com_outro:
            obs.append("%d de %d clusters também usados por outro apagado mais recente" % (com_outro, len(clus)))
        if zerados:
            obs.append("%d de %d clusters amostrados em zeros" % (zerados, len(amostra)))
        if motivo:
            self.nao_recuperados.append({"arquivo": a.caminho, "criado": a.criado, "bytes": a.real,
                                         "motivo": motivo})
            self.log("  - %s: %s" % (a.nome, motivo))
        return fisico, obs, motivo

    def apagados(self):
        """Vídeos apagados cuja cadeia de clusters ainda está no sistema de arquivos (registro
        liberado na $MFT; entrada 0x05 + FAT no exFAT). Recupera fragmentados, com o nome."""
        cl, base = self.vol.cluster, self.vol.offset
        candidatos = self._candidatos(lambda a: a.video)
        self.log("  %d vídeo(s) apagado(s) com cadeia no sistema de arquivos" % len(candidatos))
        k = 0
        for a in candidatos:
            if self.parar:
                return
            fisico, obs, motivo = self._triagem(a)
            if motivo:
                continue
            k += 1
            self.ao_etapa("Recuperando apagado pela cadeia: %s" % a.nome, None)
            lcn = "LCN%d-%d" % ((fisico[0][0] - base) // cl, (fisico[-1][1] - base - 1) // cl)
            ext_nome = os.path.splitext(a.nome)[1].lower() or ".mp4"
            dest = os.path.join(self._pasta("3_apagados"), "A%02d_%s_%s%s" % (k, limpo(a.nome), lcn, ext_nome))
            self._copiar_faixas(fisico, dest, a.real)
            self.excluir += fisico
            tr = self._trilha_de_arquivo(dest)
            validos = self._amostras_validas(dest, tr) if tr and tr.offsets else 0
            if tr:
                self.doadores.append(tr)
            if tr and tr.tamanhos:
                estado = ("íntegro: %d de %d quadros no lugar" % (validos, len(tr.tamanhos))
                          if validos == len(tr.tamanhos) else
                          "parcial: %d de %d quadros no lugar" % (validos, len(tr.tamanhos)))
            else:
                estado = "sem índice (moov) legível"
            if obs:
                estado += "; " + "; ".join(obs)
            info = self._ffprobe(dest)
            self._registrar(dict(origem="apagado", nome_original=a.caminho, arquivo=dest, estado=estado,
                                 faixas=fisico, criado=a.criado, quadros_validos=validos, **info))

    # ------------------------------------------------------------------ fotos
    def fotos(self):
        """Fotos JPEG (.insp da Insta360, .jpg): vivas por cópia lógica, apagadas pela cadeia."""
        v = self.vol
        cl, base = v.cluster, v.offset
        eh_foto = lambda a: a.nome.lower().endswith(EXT_FOTO)
        vivas = [a for a in v.arquivos.values() if a.em_uso and not a.dir and a.runs and eh_foto(a)]
        apag = self._candidatos(eh_foto)
        self.log("  %d foto(s) viva(s), %d apagada(s) com cadeia no sistema de arquivos" % (len(vivas), len(apag)))
        k = 0
        for a in vivas + apag:
            if self.parar:
                return
            if a.em_uso:
                ext = [(off, nb, vcn * cl) for off, nb, vcn in a.extents(cl)]
                fisico, obs, motivo, pasta, pref = mapear(ext, 0, a.real, base), [], None, "1_vivas", "FV"
            else:
                fisico, obs, motivo = self._triagem(a)
                pasta, pref = "2_apagadas", "FA"
            if motivo:
                continue
            k += 1
            self.ao_etapa("Recuperando foto: %s" % a.nome, None)
            lcn = "LCN%d-%d" % ((fisico[0][0] - base) // cl, (fisico[-1][1] - base - 1) // cl)
            ext_nome = os.path.splitext(a.nome)[1].lower()
            dest = os.path.join(self._pasta(pasta, "fotos"), "%s%02d_%s_%s%s" % (pref, k, limpo(a.nome), lcn, ext_nome))
            self._copiar_faixas(fisico, dest, a.real)
            self.excluir += fisico
            with open(dest, "rb") as f:
                est = fotos.estrutura_jpeg(f.read())
            decodificou, erros = fotos.decodificar(dest)
            if not est["ok"]:
                estado = "estrutura JPEG incompleta: %s" % est["motivo"]
            elif decodificou is None:
                estado = "estrutura JPEG completa (decodificador indisponível)"
            elif decodificou and not erros:
                estado = "íntegra: JPEG %sx%s decodifica sem erro" % (est["largura"], est["altura"])
            else:
                estado = "corrompida: %s" % ("; ".join(erros[:3]) or "não decodifica")
            if est["ok"] and est["fim_eoi"] < a.real:
                obs.append("%d bytes de metadados da câmera após o fim da imagem" % (a.real - est["fim_eoi"]))
            if obs:
                estado += "; " + "; ".join(obs)
            self._registrar(dict(origem="foto viva" if a.em_uso else "foto apagada", nome_original=a.caminho,
                                 arquivo=dest, estado=estado, faixas=fisico, criado=a.criado,
                                 largura=est["largura"], altura=est["altura"], duracao=None))

    def fotos_carving(self):
        """JPEG contíguos sem entrada no sistema de arquivos (espaço livre, cartão formatado)."""
        inicios = set()
        if self.vol:
            cl, base = self.vol.cluster, self.vol.offset
            inicios = {base + a.runs[0][0] * cl for a in self.vol.arquivos.values()
                       if a.runs and a.runs[0][0] is not None}
        ex = sorted(self.excluir)
        cands = [o for o in sorted(set(self.var.jpeg)) if o not in inicios
                 and not any(a <= o < b for a, b in ex[max(0, bisect.bisect_right(ex, (o, 1 << 62)) - 1):][:1])]
        if not cands:
            return
        self.log("  %d início(s) de JPEG fora de arquivos já tratados" % len(cands))
        k = falhas = 0
        cobertos = 0
        for o in cands:
            if self.parar:
                return
            if o < cobertos:                        # miniatura dentro de uma foto já recuperada
                continue
            n, est = 1 << 20, None
            while n <= (64 << 20):
                dado = self.img.ler(o, n)
                est = fotos.estrutura_jpeg(dado)
                if est["ok"] or len(dado) < n or "sem EOI" not in (est["motivo"] or ""):
                    break
                n *= 4
            if not est["ok"] or est["fim_eoi"] < 8192:
                falhas += 1
                continue
            k += 1
            cl = self.vol.cluster if self.vol else 512
            base = self.vol.offset if self.vol else 0
            fim = o + est["fim_eoi"]
            dest = os.path.join(self._pasta("3_carving", "fotos"), "FC%02d_CARVING_LCN%d-%d.jpg" % (
                k, (o - base) // cl, (fim - base - 1) // cl))
            self._copiar_faixas([(o, fim)], dest)
            cobertos = fim
            decodificou, erros = fotos.decodificar(dest)
            estado = ("íntegra: JPEG %sx%s decodifica sem erro" % (est["largura"], est["altura"])
                      if decodificou and not erros else
                      "corrompida: %s" % ("; ".join(erros[:3]) or "não decodifica"))
            self._registrar(dict(origem="foto carving", nome_original=None, arquivo=dest, estado=estado,
                                 faixas=[(o, fim)], largura=est["largura"], altura=est["altura"], duracao=None))
        if falhas:
            self.log("  %d início(s) de JPEG sem imagem completa e contígua (fragmentada ou sobrescrita)" % falhas)

    # ------------------------------------------------------------------ 4: carving
    def carving(self):
        inicios_vivos = set()                       # início de qualquer arquivo já tratado
        if self.vol:
            cl, base = self.vol.cluster, self.vol.offset
            for a in self.vol.arquivos.values():
                if a.runs and a.runs[0][0] is not None:
                    inicios_vivos.add(base + a.runs[0][0] * cl)
        n = 0
        for o in sorted(set(self.var.ftyp)):
            if self.parar:
                return
            if o in inicios_vivos:
                continue
            boxes, fim = mp4.cadeia_topo(self.img.ler, o, self.img.tamanho)
            if not boxes:
                self.log("  ftyp em %d sem estrutura MP4 completa (ignorado)" % o)
                continue
            n += 1
            self.ao_etapa("Carving de MP4 em LCN %d" % (o // 8192), None)
            cl = self.vol.cluster if self.vol else 512
            base = self.vol.offset if self.vol else 0
            nome = "C%02d_CARVING_LCN%d-%d.mp4" % (n, (o - base) // cl, (fim - base - 1) // cl)
            dest = os.path.join(self._pasta("4_carving"), nome)
            self._copiar_faixas([(o, fim)], dest)
            self.excluir.append((o, fim))
            tr = self._trilha_de_arquivo(dest)
            validos = self._amostras_validas(dest, tr) if tr and tr.offsets else 0
            if tr:
                self.doadores.append(tr)
            info = self._ffprobe(dest)
            estado = ("íntegro: %d de %d quadros no lugar" % (validos, len(tr.tamanhos))
                      if tr else "estrutura sem trilha de vídeo")
            self._registrar(dict(origem="carving", nome_original=None, arquivo=dest, estado=estado,
                                 faixas=[(o, fim)], **info))

    # ------------------------------------------------------------------ 4: fluxos sem índice
    def fluxos(self):
        hits = np.sort(np.asarray(self.var.aud, np.int64))
        if not len(hits):
            return
        if self.excluir:
            ex = np.asarray(sorted(self.excluir), np.int64)
            i = np.searchsorted(ex[:, 0], hits, side="right") - 1
            dentro = (i >= 0) & (hits < ex[np.maximum(i, 0), 1])
            hits = hits[~dentro]
        if not len(hits):
            return
        cortes = np.nonzero(np.diff(hits) > (4 << 20))[0]
        grupos = np.split(hits, cortes + 1)
        n = 0
        for g in grupos:
            if self.parar:
                return
            if len(g) < 30:
                continue
            n += 1
            self._um_fluxo(n, g)

    def _um_fluxo(self, n, hits):
        ler, lim = self.img.ler, self.img.tamanho
        self.ao_etapa("Remontando fluxo H.264 sem índice (%d quadros candidatos)" % len(hits), None)
        nals, coberto, faixas = [], -1, []
        for h in hits:
            h = int(h)
            if h < coberto:
                continue
            cad = mp4.cadeia_avcc(ler, h, lim)
            if not cad:
                continue
            nals.extend(cad)
            fim = cad[-1][0] + cad[-1][1]
            faixas.append((h, fim))
            coberto = fim
        tipos_nal = [ler(o, 1)[0] & 0x1F for o, ln in nals]
        # com parâmetros próprios, o que vem antes do primeiro SPS é resto de outro vídeo
        if 7 in tipos_nal:
            k = tipos_nal.index(7)
            while k > 0 and tipos_nal[k - 1] == 9:     # mantém o AUD do quadro do SPS
                k -= 1
            nals, tipos_nal = nals[k:], tipos_nal[k:]
            while faixas and faixas[0][1] <= nals[0][0]:
                faixas.pop(0)
        quadros = tipos_nal.count(9)
        if quadros < 30:
            return
        tipos = set(tipos_nal[:4000])
        fps_sps = 0.0
        if 7 in tipos_nal:
            o, ln = nals[tipos_nal.index(7)]
            _, _, fps_sps = mp4.sps_info(ler(o, ln))
        tmp = os.path.join(self.saida, "videos", "_tmp")
        os.makedirs(tmp, exist_ok=True)
        cabecalho, doador = b"", None
        if not ({7, 8} <= tipos):
            doador = self._escolher_doador(nals, tmp)
            if doador is None:
                self.log("  fluxo %d: sem SPS/PPS próprio e nenhum doador decodificou; não convertido" % n)
                return
            cabecalho = b"".join(b"\x00\x00\x00\x01" + x for x in doador.sps + doador.pps)
        h264 = os.path.join(tmp, "fluxo%02d.h264" % n)
        with open(h264, "wb") as out:
            out.write(cabecalho)
            for o, ln in nals:
                out.write(b"\x00\x00\x00\x01")
                out.write(ler(o, ln))
        fps = fps_sps or (doador.fps if doador and doador.fps else 0) or 29.97
        self.log("  fluxo %d: %d quadros a %.3f fps (%s)" % (
            n, quadros, fps, "SPS/VUI" if fps_sps else "doador" if doador and doador.fps else "padrão"))
        cl = self.vol.cluster if self.vol else 512
        base = self.vol.offset if self.vol else 0
        a, b = faixas[0][0], faixas[-1][1]
        aac, est_audio = self._audio_do_fluxo(n, faixas, quadros / fps, tmp)
        prov = os.path.join(tmp, "fluxo%02d.mp4" % n)
        cmd = [self.ffmpeg, "-v", "error", "-y", "-fflags", "+genpts", "-r", "%.5f" % fps,
               "-f", "h264", "-i", h264]
        if aac:
            cmd += ["-f", "aac", "-i", aac, "-map", "0:v", "-map", "1:a"]
        subprocess.run(cmd + ["-c", "copy", prov], capture_output=True, creationflags=_SEM_JANELA)
        os.remove(h264)
        if aac:
            os.remove(aac)
        if not os.path.isfile(prov):
            self.log("  fluxo %d: FFmpeg não gerou MP4" % n)
            return
        info = self._ffprobe(prov)
        sufixo = ("com-audio-%d%%" % round(est_audio["percentual"]) if aac else "sem-audio")
        nome = "F%02d_FLUXO-SEM-INDICE_LCN%d-%d_%sx%s_%s_%s.mp4" % (
            n, (a - base) // cl, (b - base - 1) // cl, info.get("largura"), info.get("altura"),
            mmss(info.get("duracao")), sufixo)
        dest = os.path.join(self._pasta("5_fluxos"), nome)
        os.replace(prov, dest)
        estado = "remontado de %d trechos; %s; %s" % (
            len(faixas), "parâmetros do próprio fluxo" if not doador else
            "parâmetros SPS/PPS de outro vídeo do dispositivo (%dx%d)" % (doador.largura, doador.altura),
            "áudio %.1f%% (%s)" % (est_audio["percentual"], est_audio["perfil"]) if aac else
            "sem áudio: %s" % est_audio.get("motivo", "-"))
        self._registrar(dict(origem="fluxo", nome_original=None, arquivo=dest, estado=estado,
                             faixas=faixas, quadros_candidatos=quadros, audio=est_audio, **info))

    def _audio_do_fluxo(self, n, faixas, duracao_video, tmp):
        """Blocos de áudio = lacunas entre os trechos de vídeo. Devolve (.aac | None, estatísticas)."""
        blocos = []
        for (_, fim), (ini, _) in zip(faixas, faixas[1:]):
            if 0 < ini - fim <= (4 << 20):
                blocos.append(self.img.ler(fim, ini - fim))
        if len(blocos) < 3:
            return None, {"motivo": "sem blocos de áudio entre os trechos de vídeo"}
        # perfil: o dos outros vídeos do dispositivo que resolver mais blocos; senão, estimado
        teste = blocos[:30]
        perfil, melhor = None, 0
        for p in self.perfis_audio:
            dec = audio.decodificador(p)
            ok = sum(1 for bl in teste if audio.dividir(bl, p, dec))
            self.log("  fluxo %d, áudio: perfil %s resolve %d de %d blocos de teste" % (n, p, ok, len(teste)))
            if ok > melhor:
                perfil, melhor = p, ok
        if melhor < len(teste) * 0.4:
            estimado = audio.estimar_perfil(blocos)
            if estimado:
                q = sum(max(1, round(len(bl) / estimado.media)) for bl in blocos)
                taxa = q * 1024 / duracao_video if duracao_video else 48000
                estimado.taxa = min(audio.TAXAS, key=lambda t: abs(t - taxa))
                self.log("  fluxo %d, áudio: perfil estimado %s" % (n, estimado))
                perfil = estimado
            elif perfil is None or melhor == 0:
                return None, {"motivo": "nenhum perfil CBR divide os blocos (áudio VBR ou ausente)"}
        sil = audio.quadro_silencio(self.ffmpeg, perfil, tmp)
        aac = os.path.join(tmp, "fluxo%02d.aac" % n)
        est = audio.escrever_adts(blocos, perfil, aac, sil, self.log)
        est["perfil"] = repr(perfil)
        self.log("  fluxo %d, áudio: %.1f%% dos bytes, %d quadros (%d de silêncio, %d fora do padrão)" % (
            n, est["percentual"], est["quadros"], est["quadros_silencio"], est["quadros_fora_do_padrao"]))
        if est["percentual"] <= 0:
            os.remove(aac)
            return None, {"motivo": "nenhum bloco de áudio pôde ser dividido em quadros"}
        return aac, est

    def _escolher_doador(self, nals, tmp):
        """Testa o SPS/PPS de cada vídeo conhecido nos primeiros quadros do fluxo."""
        melhor, melhor_ok = None, 0
        amostra = nals[:3000]
        corpo = b"".join(b"\x00\x00\x00\x01" + self.img.ler(o, ln) for o, ln in amostra)
        vistos = set()
        for d in self.doadores:
            if not d.sps or not d.pps:
                continue
            chave = (d.sps[0], d.pps[0])
            if chave in vistos:
                continue
            vistos.add(chave)
            t = os.path.join(tmp, "teste.h264")
            with open(t, "wb") as f:
                f.write(b"".join(b"\x00\x00\x00\x01" + x for x in d.sps + d.pps) + corpo)
            r = subprocess.run([self.ffmpeg, "-v", "error", "-f", "h264", "-i", t, "-f", "framemd5",
                                "-"], capture_output=True, text=True, creationflags=_SEM_JANELA)
            ok = sum(1 for l in r.stdout.splitlines() if l and not l.startswith("#"))
            erros = len(r.stderr.splitlines())
            nota = ok - 5 * erros
            self.log("  doador %dx%d: %d quadros decodificados, %d erros" % (d.largura, d.altura, ok, erros))
            if nota > melhor_ok:
                melhor, melhor_ok = d, nota
            os.remove(t)
        return melhor

    # ------------------------------------------------------------------ orquestração
    def executar(self):
        os.makedirs(self.saida, exist_ok=True)
        self.audit = open(os.path.join(self.saida, "auditoria.jsonl"), "a", encoding="utf-8")
        self._auditar("inicio_recuperacao", versao=VERSAO, imagem=self.img.caminho,
                      segmentos=self.img.segmentos, ffmpeg=self.ffmpeg)
        try:
            if not self.ffmpeg:
                self.log("AVISO: FFmpeg não encontrado; fluxos sem índice não serão convertidos.")
            if self.vol:
                self.log("1-2. Vídeos no sistema de arquivos (%s)" % self.vol.tipo)
                self.vivos_e_restaurados()
                self.log("3. Vídeos apagados pela cadeia do sistema de arquivos")
                self.apagados()
                self.log("   Fotos (JPEG / .insp)")
                self.fotos()
            self.log("4. Carving de MP4/MOV completos")
            self.carving()
            self.log("   Carving de fotos JPEG")
            self.fotos_carving()
            if self.ffmpeg:
                self.log("5. Fluxos H.264 sem índice")
                self.fluxos()
            tmp = os.path.join(self.saida, "videos", "_tmp")
            if os.path.isdir(tmp) and not os.listdir(tmp):
                os.rmdir(tmp)
            self._relatorio()
        finally:
            self._auditar("fim_recuperacao", itens=len(self.itens))
            self.audit.close()
        return self.itens

    def _relatorio(self):
        v = self.var
        rel = {
            "programa": "Vestigo %s" % VERSAO,
            "gerado_em": datetime.datetime.now().isoformat(timespec="seconds"),
            "imagem": {"arquivo": self.img.caminho, "tipo": self.img.tipo,
                       "segmentos": len(self.img.segmentos), "bytes": self.img.tamanho},
            "integridade": {"md5_calculado": v.md5, "sha1_calculado": v.sha1,
                            "gravados_na_aquisicao": self.img.hashes_gravados,
                            "hash_incompleto": v.hash_incompleto,
                            "confere": (not v.hash_incompleto and bool(self.img.hashes_gravados) and
                                        self.img.hashes_gravados.get("MD5") in (None, v.md5) and
                                        self.img.hashes_gravados.get("SHA-1") in (None, v.sha1))},
            "trechos_ilegiveis": [{"inicio": a, "fim": b, "bytes": b - a} for a, b in self.img.falhas],
            "itens": [{k: val for k, val in it.items()} for it in self.itens],
            "nao_recuperados": self.nao_recuperados,
        }
        with open(os.path.join(self.saida, "relatorio.json"), "w", encoding="utf-8") as f:
            json.dump(rel, f, ensure_ascii=False, indent=2)
        linhas = ["RELATÓRIO DE RECUPERAÇÃO — Vestigo %s" % VERSAO, "",
                  "Imagem : %s" % self.img.caminho,
                  "Bytes  : %d" % self.img.tamanho,
                  "MD5    : %s  (aquisição: %s)" % (v.md5, self.img.hashes_gravados.get("MD5", "-")),
                  "SHA-1  : %s  (aquisição: %s)" % (v.sha1, self.img.hashes_gravados.get("SHA-1", "-")),
                  "Integridade: %s" % ("CONFERE" if rel["integridade"]["confere"] else
                                       "NÃO VERIFICADA / DIVERGE"), ""]
        if self.img.falhas:
            linhas[-1:-1] = ["ATENÇÃO: %d trecho(s) ilegível(is), %s bytes, lidos como zeros. Os hashes "
                             "acima NÃO são os do dispositivo." % (
                                 len(self.img.falhas), self.img.bytes_ilegiveis())] + [
                "  bytes %d – %d" % (a, b) for a, b in self.img.falhas[:50]]
        for it in self.itens:
            linhas += ["[%d] %s" % (it["id"], os.path.relpath(it["arquivo"], self.saida)),
                       "     origem: %s   estado: %s" % (it["origem"], it["estado"]),
                       "     duração: %s   %sx%s   %s bytes" % (mmss(it.get("duracao")), it.get("largura"),
                                                             it.get("altura"), it["tamanho"]),
                       "     SHA-256: %s" % it["sha256"], ""]
        if self.nao_recuperados:
            linhas += ["APAGADOS LOCALIZADOS E NÃO RECUPERADOS (%d)" % len(self.nao_recuperados)]
            for x in self.nao_recuperados:
                linhas.append("  %s  (criado %s, %s bytes): %s" % (x["arquivo"], x["criado"], x["bytes"],
                                                                  x["motivo"]))
        with open(os.path.join(self.saida, "relatorio.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(linhas))


_SEM_JANELA = 0x08000000 if os.name == "nt" else 0
