# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Conversão de arquivos recuperados para formatos de uso comum.

Nenhuma conversão toca no original: o resultado vai para outra pasta, marcado como derivado.
Duas famílias, e a diferença importa para a prova:

  SEM PERDA   'mp4' (remux: troca só o recipiente, fluxos copiados bit a bit) e 'jpg' (a imagem
              JPEG cortada no fim, sem o trailer da câmera). O conteúdo audiovisual é o mesmo.
  RECODIFICA  'lado_a_lado' (as duas lentes de uma câmera 360° numa só imagem) e
              'equiretangular' (as duas lentes desdobradas em vídeo 360°). São CÓPIAS DE
              VISUALIZAÇÃO: a costura 360° aqui é geométrica e aproximada — a oficial do
              fabricante usa a calibração e o giroscópio de cada câmera.
"""
import json
import os
import subprocess

from . import fotos

SEM_JANELA = 0x08000000 if os.name == "nt" else 0
EXT_CONTEINER_MP4 = (".mp4", ".mov", ".m4v", ".3gp", ".insv", ".lrv")
FOV_PADRAO = 193.0           # campo de visão típico das lentes olho de peixe de câmeras 360°

ROTULOS = {
    "mp4": ("MP4 (sem perda: só troca o recipiente)", False),
    "lado_a_lado": ("MP4 com as duas lentes lado a lado (recodifica)", True),
    "equiretangular": ("MP4 360° equiretangular, costura aproximada (recodifica)", True),
    "jpg": ("JPG (sem perda: a imagem sem o trailer da câmera)", False),
}


def _sonda(ffprobe, caminho):
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                        "stream=index,codec_type,codec_name,width,height", "-of", "json", caminho],
                       capture_output=True, text=True, creationflags=SEM_JANELA)
    try:
        return json.loads(r.stdout).get("streams", [])
    except ValueError:
        return []


def opcoes(caminho, ffprobe):
    """Conversões possíveis para este arquivo: [(chave, rótulo, recodifica)]."""
    ext = os.path.splitext(caminho)[1].lower()
    if ext in fotos.EXT_FOTO:
        return [("jpg",) + ROTULOS["jpg"]] if ext != ".jpg" else []
    if not ffprobe:
        return []
    videos = [s for s in _sonda(ffprobe, caminho) if s.get("codec_type") == "video"]
    if not videos:
        return []
    out = []
    if ext != ".mp4":
        out.append(("mp4",) + ROTULOS["mp4"])
    duas_lentes = (len(videos) >= 2 and videos[0].get("width") == videos[0].get("height")
                   and videos[0].get("width") == videos[1].get("width"))
    if duas_lentes:
        out += [(k,) + ROTULOS[k] for k in ("lado_a_lado", "equiretangular")]
    elif videos[0].get("width") and videos[0].get("width") == 2 * videos[0].get("height", 0) and ext == ".lrv":
        out.append(("equiretangular",) + ROTULOS["equiretangular"])   # proxy já lado a lado
    return out


_codificadores = {}


def codificadores(ffmpeg):
    """Codificadores disponíveis, placa de vídeo antes da CPU. Um codificador de hardware pode
    abrir aqui e recusar a resolução real (o Quick Sync testado recusou 5760×2880): por isso a
    conversão tenta a lista em ordem, e a CPU (libx265, libx264) fica sempre no fim."""
    if ffmpeg not in _codificadores:
        ok = []
        for enc in ("hevc_nvenc", "hevc_qsv", "hevc_amf"):
            r = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=640x480:d=0.1",
                                "-frames:v", "1", "-c:v", enc, "-f", "null", "-"],
                               capture_output=True, creationflags=SEM_JANELA)
            if r.returncode == 0:
                ok.append(enc)
        _codificadores[ffmpeg] = ok + ["libx265", "libx264"]
    return _codificadores[ffmpeg]


def _qualidade(enc):
    return {"hevc_nvenc": ["-preset", "p5", "-cq", "20"], "hevc_qsv": ["-global_quality", "20"],
            "hevc_amf": ["-quality", "quality", "-qp_i", "20", "-qp_p", "22"],
            "libx265": ["-preset", "medium", "-crf", "20"], "libx264": ["-preset", "medium", "-crf", "18"]}[enc]


def converter(caminho, chave, pasta, ffmpeg, ffprobe, fov=FOV_PADRAO, segundos=None, largura=3840):
    """largura: das cópias recodificadas (altura = metade). 3840 é o 4K 360° padrão dos players e
    cabe no codificador de hardware; 0 = resolução cheia das lentes (5760 numa câmera 2×2880),
    que em CPU leva dezenas de minutos por minuto de vídeo."""
    """Converte e devolve {arquivo, comando, recodifica, observacao}. Não altera o original."""
    os.makedirs(pasta, exist_ok=True)
    base = os.path.splitext(os.path.basename(caminho))[0]
    recodifica = ROTULOS[chave][1]
    if chave == "jpg":
        with open(caminho, "rb") as f:
            dado = f.read()
        est = fotos.estrutura_jpeg(dado)
        if not est["ok"]:
            raise ValueError("não é um JPEG íntegro: %s" % est["motivo"])
        dest = os.path.join(pasta, base + ".jpg")
        with open(dest, "wb") as f:
            f.write(dado[:est["fim_eoi"]])
        sobra = len(dado) - est["fim_eoi"]
        return {"arquivo": dest, "comando": "cópia dos bytes 0–%d (até o EOI)" % est["fim_eoi"],
                "recodifica": False,
                "observacao": "imagem idêntica; %d bytes de metadados da câmera ficam só no original" % sobra}
    sufixo = {"mp4": "", "lado_a_lado": "_lado-a-lado", "equiretangular": "_360-equiretangular"}[chave]
    dest = os.path.join(pasta, base + sufixo + ".mp4")
    cmd = [ffmpeg, "-v", "error", "-y", "-i", caminho]
    if segundos:
        cmd += ["-t", str(segundos)]
    if chave == "mp4":
        cmd += ["-map", "0:v", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart"]
        obs = "fluxos copiados bit a bit; metadados próprios do fabricante ficam só no original"
        return _executar(cmd + [dest], dest, recodifica, obs)
    tam = ":w=%d:h=%d" % (largura, largura // 2) if largura else ""
    v360 = "v360=input=dfisheye:output=e:ih_fov=%g:iv_fov=%g%s" % (fov, fov, tam)
    escala = ",scale=%d:%d" % (largura, largura // 2) if largura else ""
    duas = len([s for s in _sonda(ffprobe, caminho) if s.get("codec_type") == "video"]) >= 2
    if chave == "lado_a_lado":
        grafo = "[0:v:0][0:v:1]hstack=inputs=2" + escala + "[v]"
    elif duas:
        grafo = "[0:v:0][0:v:1]hstack=inputs=2," + v360 + "[v]"
    else:
        grafo = "[0:v:0]" + v360 + "[v]"
    erros = []
    for enc in codificadores(ffmpeg):
        c = cmd + ["-filter_complex", grafo, "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
                   "-c:v", enc] + _qualidade(enc) + ["-tag:v", "avc1" if enc == "libx264" else "hvc1",
                                                     "-movflags", "+faststart", dest]
        obs = ("CÓPIA DE VISUALIZAÇÃO recodificada (%s)%s; para prova, vale o original" % (
            enc, "; costura 360° geométrica aproximada, campo de visão %g°" % fov
            if chave == "equiretangular" else ""))
        try:
            return _executar(c, dest, True, obs)
        except RuntimeError as e:
            erros.append("%s: %s" % (enc, e))
    raise RuntimeError("nenhum codificador conseguiu: " + " | ".join(erros))


def _executar(cmd, dest, recodifica, obs):
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=SEM_JANELA)
    if r.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        if os.path.isfile(dest):
            os.remove(dest)
        raise RuntimeError((r.stderr.strip().splitlines() or ["?"])[-1])
    return {"arquivo": dest, "comando": " ".join('"%s"' % c if " " in c else c for c in cmd[1:]),
            "recodifica": recodifica, "observacao": obs}
