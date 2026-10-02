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
    ap.add_argument("--converter", default="",
                    help="ao fim, converte o que for compatível: mp4, jpg, lado_a_lado, equiretangular "
                         "(separados por vírgula). mp4 e jpg não recodificam; os outros geram cópias "
                         "de visualização")
    ap.add_argument("--resolucao-cheia", action="store_true",
                    help="cópias recodificadas na resolução das lentes, em vez de 3840×1920 (lento)")
    args = ap.parse_args()

    if args.sem_interface:
        if not args.imagem or not args.saida:
            ap.error("--sem-interface exige a imagem e --saida")
        pasta_img = os.path.normcase(os.path.dirname(os.path.abspath(args.imagem)))
        alvo = os.path.normcase(os.path.abspath(args.saida))
        if alvo == pasta_img or alvo.startswith(pasta_img + os.sep):
            ap.error("a pasta de saída não pode ficar dentro da pasta da imagem forense")
        conv = [c.strip() for c in args.converter.split(",") if c.strip()]
        invalidas = [c for c in conv if c not in ("mp4", "jpg", "lado_a_lado", "equiretangular")]
        if invalidas:
            ap.error("formato de conversão desconhecido: %s" % ", ".join(invalidas))
        return terminal(args.imagem, args.saida, conv, 0 if args.resolucao_cheia else 3840)
    from .gui import abrir_interface
    return abrir_interface(args.imagem, args.saida, args.iniciar)


def terminal(caminho, saida, converter=(), largura=3840):
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
    if converter:
        from .conversao import opcoes
        for it in list(rec.itens):
            possiveis = {o[0] for o in opcoes(it["arquivo"], rec.ffprobe)}
            for chave in converter:
                if chave in possiveis:
                    print("  convertendo %s -> %s" % (os.path.basename(it["arquivo"]), chave))
                    try:
                        rec.converter_arquivo(it["arquivo"], chave, largura)
                    except Exception as e:                   # noqa: BLE001 - registrar e seguir
                        print("    falhou: %s" % e)
        print("%d arquivo(s) no total, com as conversões" % len(rec.itens))


if __name__ == "__main__":
    sys.exit(main())
