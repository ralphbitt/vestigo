# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Abre a imagem, lê o sistema de arquivos e define onde procurar fluxos sem índice."""
from .exfat import VolumeExFAT
from .imagem import Imagem
from .ntfs import VolumeNTFS, localizar_volumes

CLASSES = {"NTFS": VolumeNTFS, "exFAT": VolumeExFAT}


class Sessao:
    def __init__(self, caminho):
        self.img = Imagem(caminho)
        self.vol = None
        self.aviso = None
        vols = localizar_volumes(self.img)
        if not vols:
            self.aviso = ("Nenhum sistema de arquivos reconhecido (NTFS/exFAT): só carving e busca "
                          "de fluxos na imagem inteira.")
        else:
            off, tipo = vols[0]
            try:
                self.vol = CLASSES[tipo](self.img, off)
            except Exception as e:                   # sistema de arquivos corrompido
                self.aviso = "%s ilegível (%s): só carving e busca de fluxos." % (tipo, e)
        self.busca = self._intervalos_busca()

    def _intervalos_busca(self):
        """Onde procurar vídeo sem índice: a mídia inteira, menos o conteúdo dos vídeos vivos e
        íntegros (que já são exportados como estão).

        Não basta o espaço livre: parte de um vídeo apagado pode sobreviver dentro de clusters
        alocados a outro arquivo que foi alocado mas pouco escrito."""
        total = self.img.tamanho
        if not self.vol:
            return [(0, total)]
        v, base = self.vol, self.vol.offset
        fora = []
        for a in v.arquivos.values():
            if a.em_uso and a.video and not a.danificado:
                fora += [(base + o, base + o + n) for o, n, _ in a.extents(v.cluster)]
        out, p = [], 0
        for a, b in sorted(fora):
            if a > p:
                out.append((p, a))
            p = max(p, b)
        if p < total:
            out.append((p, total))
        return out
