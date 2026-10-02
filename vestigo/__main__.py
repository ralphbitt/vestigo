# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Vestigo: mapa físico do dispositivo e recuperação de vídeos.

    python -m vestigo <primeiro arquivo da imagem> [--saida PASTA] [--iniciar]
    python -m vestigo <imagem> --saida PASTA --sem-interface
"""
import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser(prog="vestigo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("imagem", nargs="?", help="primeiro arquivo da imagem (.E01, .001, .dd, .raw)")
    ap.add_argument("--saida", help="pasta de saída dos vídeos recuperados e do relatório")
    ap.add_argument("--iniciar", action="store_true", help="começa a varredura ao abrir")
    ap.add_argument("--sem-interface", action="store_true", help="executa no terminal, sem janela")
    args = ap.parse_args()

    if args.sem_interface:
        if not args.imagem or not args.saida:
            ap.error("--sem-interface exige a imagem e --saida")
        pasta_img = os.path.normcase(os.path.dirname(os.path.abspath(args.imagem)))
        alvo = os.path.normcase(os.path.abspath(args.saida))
        if alvo == pasta_img or alvo.startswith(pasta_img + os.sep):
            ap.error("a pasta de saída não pode ficar dentro da pasta da imagem forense")
        return terminal(args.imagem, args.saida)
    from .gui import abrir_interface
    return abrir_interface(args.imagem, args.saida, args.iniciar)


def terminal(caminho, saida):
    sys.stdout.reconfigure(encoding="utf-8")
    from .recuperacao import Recuperador
    from .sessao import Sessao
    from .varredura import Varredura
    s = Sessao(caminho)
    print(s.img.descricao())
    if s.aviso:
        print(s.aviso)
    t0, ult = time.time(), [0.0]

    def prog(v):
        if time.time() - ult[0] > 2:
            ult[0] = time.time()
            print("  varredura %5.1f%%  %4.0f MB/s" % (100 * v.pos / v.total, v.velocidade / 1e6))

    v = Varredura(s.img, s.busca).executar(prog)
    print("varredura: %.0f s · MD5 %s · SHA-1 %s" % (time.time() - t0, v.md5, v.sha1))
    rec = Recuperador(s.img, s.vol, v, os.path.abspath(saida))
    itens = rec.executar()
    print("%d arquivo(s) em %s" % (len(itens), saida))


if __name__ == "__main__":
    sys.exit(main())
