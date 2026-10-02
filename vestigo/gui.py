# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Interface gráfica: mapa físico do dispositivo com cursor de varredura animado."""
import os
import sys
import time

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, Signal, QUrl
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont, QFontMetrics,
                           QLinearGradient, QPainter, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QScrollArea,
                               QSplitter, QTableWidget, QTableWidgetItem, QToolBar, QToolTip,
                               QVBoxLayout, QWidget)

from .recuperacao import Recuperador, VERSAO, mmss
from .sessao import Sessao
from .varredura import Varredura

# paleta do mapa físico
FUNDO = QColor("#1b1d22")
GRADE = QColor("#2c2f37")
TEXTO = QColor("#e6e6e6")
TEXTO2 = QColor("#8c93a0")
GRUPO = QColor("#9aa3b2")
COR = {
    "meta": QColor("#5b82b0"), "integro": QColor("#4aa383"), "danificado": QColor("#d59a2f"),
    "nao_init": QColor("#6e5634"), "livre": QColor("#3a3e48"), "apagado": QColor("#c8a55a"),
    "recuperado": QColor("#8b6cc8"), "destruido": QColor("#c9564c"), "outros": QColor("#3f6f5e"),
    "zeros": QColor("#24272d"), "ff": QColor("#5a3a40"), "dados": QColor("#6f7784"),
    "video": QColor("#b394ff"), "cursor": QColor("#ffd24a"), "ilegivel": QColor("#ff3b3b"),
}
ROTULO_W, LINHA_H, GRUPO_H, TOPO_H = 330, 24, 30, 92


class Linha:
    def __init__(self, rotulo, segs=(), cor="integro", nota="", recuo=0, estilo="cheio", tip=""):
        self.rotulo, self.segs, self.cor, self.nota = rotulo, list(segs), cor, nota
        self.recuo, self.estilo, self.tip = recuo, estilo, tip


def gib(n):
    return "%.2f GiB" % (n / 2 ** 30) if n >= 2 ** 30 else "%.1f MiB" % (n / 2 ** 20)


def montar_grupos(s):
    """Linhas do mapa a partir do sistema de arquivos (mesma organização do mapa físico)."""
    grupos = []
    v = s.vol
    if v:
        base, cl = v.offset, v.cluster

        def fis(a):
            return [(base + o, base + o + n) for o, n, _ in a.extents(cl)]

        meta, outros, videos, apagados = [], [], [], []
        for a in v.arquivos.values():
            if not a.runs and not a.indice_runs:
                continue
            if a.metadado or a.dir:
                meta += fis(a) + [(base + l * cl, base + (l + n) * cl) for l, n in a.indice_runs if l]
            elif not a.em_uso:
                apagados.append(a)
            elif a.video:
                videos.append(a)
            else:
                outros.append(a)
        rot_meta = ("Metadados: $MFT, $LogFile, $Bitmap, índices…" if v.tipo == "NTFS" else
                    "Metadados: FAT, bitmap, maiúsculas, diretórios")
        g = [Linha(rot_meta, meta, "meta", gib(sum(b - a for a, b in meta)))]
        grupos.append(("METADADOS DO %s" % v.tipo.upper(), g))
        g = []
        videos.sort(key=lambda a: a.runs[0][0] or 0)
        for a in videos[:30]:
            segs = fis(a)
            if a.danificado:
                g.append(Linha(a.nome, segs, "danificado", "%s declarados · só %s gravados" % (
                    gib(a.real), gib(a.init)), tip=a.caminho))
                ok, nao = [], []
                for off, nb, vcn in a.extents(cl):
                    l0, lim = vcn * cl, a.init
                    if lim > l0:
                        ok.append((base + off, base + off + min(nb, lim - l0)))
                    if lim < l0 + nb:
                        nao.append((base + off + max(0, lim - l0), base + off + nb))
                g.append(Linha("└ parte gravada (até InitializedSize)", ok, "integro", gib(a.init), 1))
                g.append(Linha("└ alocado e NÃO inicializado", nao, "nao_init",
                               "%s · NTFS devolve zeros" % gib(a.real - a.init), 1))
            else:
                g.append(Linha(a.nome, segs, "integro", gib(a.real), tip=a.caminho))
        if len(videos) > 30:
            resto = [x for a in videos[30:] for x in fis(a)]
            g.append(Linha("outros %d vídeos" % (len(videos) - 30), resto, "integro"))
        if outros:
            segs = [x for a in outros for x in fis(a)]
            g.append(Linha("outros arquivos (%d)" % len(outros), segs, "outros",
                           gib(sum(b - a for a, b in segs))))
        grupos.append(("ARQUIVOS VIVOS", g))
        if apagados:
            apagados.sort(key=lambda a: (not a.video, a.criado or ""))    # vídeos primeiro
            origem = "registro $MFT %d liberado" if v.tipo == "NTFS" else "entrada de diretório apagada"
            g = [Linha(a.nome or "registro %d" % a.num, fis(a), "apagado", gib(a.real),
                       estilo="tracejado", tip=(origem % a.num if "%d" in origem else origem) +
                       "\n" + a.caminho + ("\ncriado %s" % a.criado if a.criado else ""))
                 for a in apagados[:20]]
            if len(apagados) > 20:
                g.append(Linha("outros %d apagados" % (len(apagados) - 20),
                               [x for a in apagados[20:] for x in fis(a)], "apagado", estilo="tracejado"))
            titulo = ("registros liberados na $MFT" if v.tipo == "NTFS" else
                      "entradas apagadas, com a cadeia da FAT")
            grupos.append(("ARQUIVOS APAGADOS — %s (%d)" % (titulo, len(apagados)), g))
        livres = [(base + o, base + o + n) for o, n in v.livres()]
        grupos.append(("ESPAÇO NÃO ALOCADO", [Linha("livre segundo o bitmap de alocação", livres, "livre",
                                                    gib(sum(b - a for a, b in livres)),
                                                    estilo="tracejado")]))
    return grupos


class MapaWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.sessao = None
        self.grupos = []
        self.recuperados = []
        self.var = None
        self.cursor_real = 0.0
        self.cursor_vis = 0.0
        self.fase = 0.0
        self.alvos = []
        self.setMouseTracking(True)
        self.setMinimumWidth(1100)
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tique)
        self._anim.start(33)

    def carregar(self, sessao):
        self.sessao = sessao
        self.grupos = montar_grupos(sessao)
        self.recuperados = []
        self.var = None
        self.cursor_real = self.cursor_vis = 0.0
        self._ajustar_altura()
        self.update()

    def adicionar_recuperado(self, it):
        cor = "recuperado"
        rot = os.path.basename(it["arquivo"])
        self.recuperados.append(Linha(rot[:44], it.get("faixas") or [], cor,
                                      "%s · %s" % (mmss(it.get("duracao")), it["origem"]), tip=rot))
        self._ajustar_altura()
        self.update()

    def _linhas(self):
        g = list(self.grupos)
        g.append(("LEITURA DA MÍDIA", [Linha("conteúdo lido (zeros · 0xFF · dados)", (), "dados"),
                                       Linha("assinaturas de vídeo encontradas", (), "video")]))
        g.append(("MATERIAL RECUPERADO", self.recuperados or
                  [Linha("(aparece aqui durante a recuperação)", (), "recuperado")]))
        return g

    def _ajustar_altura(self):
        n = sum(GRUPO_H + LINHA_H * len(ls) for _, ls in self._linhas())
        self.setMinimumHeight(TOPO_H + n + 70)

    def _tique(self):
        self.fase = (self.fase + 0.08) % 6.283
        if abs(self.cursor_vis - self.cursor_real) > 0.5:
            self.cursor_vis += (self.cursor_real - self.cursor_vis) * 0.25
        self.update()

    # ------------------------------------------------------------------ desenho
    def _x(self, off):
        total = self.sessao.img.tamanho
        return ROTULO_W + (self.width() - ROTULO_W - 30) * off / total

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), FUNDO)
        if not self.sessao:
            p.setPen(TEXTO2)
            p.setFont(QFont("Segoe UI", 12))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Abra o primeiro arquivo da imagem (.E01, .001, .dd) para carregar o mapa físico.")
            return
        self.alvos = []
        img = self.sessao.img
        W = self.width()
        pw = W - ROTULO_W - 30
        # título
        p.setPen(TEXTO)
        p.setFont(QFont("Segoe UI", 15, QFont.DemiBold))
        p.drawText(20, 32, "Mapa físico do dispositivo — %s bytes" % format(img.tamanho, ",").replace(",", "."))
        p.setFont(QFont("Segoe UI", 9))
        p.setPen(TEXTO2)
        sub = img.descricao()
        if self.sessao.vol:
            v = self.sessao.vol
            sub += " · %s em %d · cluster de %d B" % (v.tipo, v.inicio, v.cluster)
        if self.sessao.aviso:
            sub += " · " + self.sessao.aviso
        p.drawText(20, 52, sub)
        # eixo
        passo = max(1, int(round(img.tamanho / 2 ** 30 / 14)))
        y0, y1 = TOPO_H - 8, self.height() - 60
        f8 = QFont("Segoe UI", 8)
        p.setFont(f8)
        k = 0
        while k * 2 ** 30 <= img.tamanho:
            x = self._x(k * 2 ** 30)
            p.setPen(QPen(GRADE, 1))
            p.drawLine(QPointF(x, y0), QPointF(x, y1))
            p.setPen(TEXTO2)
            p.drawText(QRectF(x - 40, 62, 80, 14), Qt.AlignCenter, "%d GiB" % k)
            k += passo
        y = TOPO_H
        for nome, linhas in self._linhas():
            p.setPen(GRUPO)
            p.setFont(QFont("Segoe UI", 9, QFont.Bold))
            p.drawText(20, y + 20, nome)
            p.setPen(QPen(GRADE, 1))
            p.drawLine(20, y + GRUPO_H - 3, W - 30, y + GRUPO_H - 3)
            y += GRUPO_H
            for ln in linhas:
                self._linha(p, ln, y, pw)
                y += LINHA_H
        self._legenda(p, y + 16)
        self._cursor(p, y0, y1)

    def _linha(self, p, ln, y, pw):
        p.setFont(QFont("Segoe UI", 9))
        p.setPen(TEXTO if not ln.recuo else TEXTO2)
        fm = QFontMetrics(p.font())
        p.drawText(20 + 16 * ln.recuo, y + 16, fm.elidedText(ln.rotulo, Qt.ElideRight, ROTULO_W - 30))
        if ln.rotulo.startswith("conteúdo lido"):
            self._faixa_leitura(p, y)
            return
        if ln.rotulo.startswith("assinaturas"):
            self._faixa_video(p, y)
            return
        cor = COR[ln.cor]
        xmax = 0
        for a, b in ln.segs:
            x0, x1 = self._x(a), self._x(b)
            r = QRectF(x0, y + 4, max(x1 - x0, 2.5), LINHA_H - 8)
            if ln.estilo == "tracejado":
                p.setPen(QPen(cor.lighter(140), 1, Qt.DashLine))
                p.setBrush(QColor(cor.red(), cor.green(), cor.blue(), 110))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(cor)
            p.drawRoundedRect(r, 3, 3)
            xmax = max(xmax, r.right())
            self.alvos.append((r, "%s\nbytes %s – %s (%s)\nLCN %s – %s%s" % (
                ln.rotulo, format(a, ","), format(b, ","), gib(b - a), self._lcn(a), self._lcn(b - 1),
                "\n" + ln.tip if ln.tip else "")))
        if ln.nota and ln.segs:
            p.setPen(TEXTO2)
            p.setFont(QFont("Segoe UI", 8))
            fm = QFontMetrics(p.font())
            larg = fm.horizontalAdvance(ln.nota)
            xn = xmax + 8 if xmax + 8 + larg < self.width() - 30 else self._x(ln.segs[0][0]) - larg - 8
            p.drawText(QPointF(xn, y + 16), ln.nota)

    def _lcn(self, off):
        v = self.sessao.vol
        return format((off - v.offset) // v.cluster, ",").replace(",", ".") if v else "-"

    def _faixa_leitura(self, p, y):
        v = self.var
        if v is None:
            return
        pw = self.width() - ROTULO_W - 30
        n = len(v.zeros)
        ate = int(n * v.pos / v.total) if v.total else 0
        cols = max(int(pw), 1)
        idx = np.linspace(0, n, cols + 1).astype(int)
        z, f, d, il = v.zeros, v.ff, v.dados, v.ilegivel
        for c in range(cols):
            a, b = idx[c], max(idx[c + 1], idx[c] + 1)
            if a >= ate:
                break
            zz, ff, dd = z[a:b].sum(), f[a:b].sum(), d[a:b].sum()
            tot = zz + ff + dd
            if not tot:
                continue
            if il[a:b].any():                        # ilegível tem precedência: é o que importa ver
                cor = COR["ilegivel"]
            else:
                cor = COR["dados"] if dd >= max(zz, ff) else (COR["ff"] if ff >= zz else COR["zeros"])
            p.fillRect(QRectF(ROTULO_W + c, y + 4, 1.2, LINHA_H - 8), cor)

    def _faixa_video(self, p, y):
        v = self.var
        if v is None:
            return
        pw = self.width() - ROTULO_W - 30
        hits = np.nonzero(v.video)[0]
        n = len(v.video)
        p.setPen(QPen(COR["video"], 1.4))
        for h in hits:
            x = ROTULO_W + pw * h / n
            p.drawLine(QPointF(x, y + 5), QPointF(x, y + LINHA_H - 5))

    def _cursor(self, p, y0, y1):
        v = self.var
        if v is None or v.concluida and self.cursor_vis >= v.total - 1:
            return
        x = self._x(self.cursor_vis)
        brilho = 0.55 + 0.45 * (0.5 + 0.5 * np.sin(self.fase * 2))
        g = QLinearGradient(x - 28, 0, x, 0)
        g.setColorAt(0, QColor(255, 210, 74, 0))
        g.setColorAt(1, QColor(255, 210, 74, int(70 * brilho)))
        p.fillRect(QRectF(x - 28, y0, 28, y1 - y0), QBrush(g))
        p.setPen(QPen(COR["cursor"], 2))
        p.drawLine(QPointF(x, y0 - 6), QPointF(x, y1))
        setor = int(self.cursor_vis // 512)
        txt = "setor %s · %.1f%%" % (format(setor, ",").replace(",", "."), 100 * self.cursor_vis / v.total)
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        fm = QFontMetrics(p.font())
        w = fm.horizontalAdvance(txt) + 12
        bx = min(max(x - w / 2, ROTULO_W), self.width() - w - 5)
        p.setBrush(COR["cursor"])
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(bx, y0 - 22, w, 16), 4, 4)
        p.setPen(QColor("#1b1d22"))
        p.drawText(QRectF(bx, y0 - 22, w, 16), Qt.AlignCenter, txt)

    def _legenda(self, p, y):
        itens = [("meta", "Metadados do sistema de arquivos"),("integro", "Arquivo íntegro"), ("danificado", "Arquivo danificado"),
                 ("nao_init", "Alocado e NÃO inicializado"), ("apagado", "Arquivo apagado"),
                 ("livre", "Espaço não alocado"), ("ff", "Lido: 0xFF (NAND apagada)"),
                 ("dados", "Lido: dados"), ("ilegivel", "Ilegível na imagem"),
                 ("video", "Assinatura de vídeo"), ("recuperado", "Recuperado")]
        x = 20
        p.setFont(QFont("Segoe UI", 8))
        fm = QFontMetrics(p.font())
        for k, t in itens:
            w = fm.horizontalAdvance(t) + 34
            if x + w > self.width() - 20:
                x, y = 20, y + 18
            p.setPen(Qt.NoPen)
            p.setBrush(COR[k])
            p.drawRoundedRect(QRectF(x, y, 12, 12), 2, 2)
            p.setPen(TEXTO2)
            p.drawText(QPointF(x + 18, y + 10), t)
            x += w

    def mouseMoveEvent(self, e):
        for r, t in self.alvos:
            if r.adjusted(-2, -2, 2, 2).contains(e.position()):
                QToolTip.showText(e.globalPosition().toPoint(), t, self)
                return
        QToolTip.hideText()


# ---------------------------------------------------------------------- trabalho em segundo plano
class Trabalho(QObject):
    progresso = Signal(object)
    log = Signal(str)
    item = Signal(dict)
    etapa = Signal(str)
    fim = Signal(object)

    def __init__(self, sessao, saida):
        super().__init__()
        self.sessao, self.saida = sessao, saida
        self.var = None
        self.rec = None
        self._ult = 0.0

    def parar(self):
        if self.var:
            self.var.parar = True
        if self.rec:
            self.rec.parar = True

    def rodar(self):
        s = self.sessao
        try:
            self.var = Varredura(s.img, s.busca)
            self.etapa.emit("Varredura setor a setor")
            self.log.emit("Varredura iniciada: %s" % s.img.descricao())

            def prog(v):
                if time.time() - self._ult > 0.05:
                    self._ult = time.time()
                    self.progresso.emit(v)

            self.var.executar(prog)
            self.progresso.emit(self.var)
            if not self.var.concluida:
                self.log.emit("Varredura interrompida pelo usuário.")
                self.fim.emit(None)
                return
            self.log.emit("Varredura concluída. MD5 %s · SHA-1 %s" % (self.var.md5, self.var.sha1))
            g = s.img.hashes_gravados
            if self.var.hash_incompleto:
                self.log.emit("ATENÇÃO: %d trecho(s) ilegível(is) (%s bytes) foram lidos como zeros; "
                              "o hash calculado não é o do dispositivo." % (
                                  len(s.img.falhas), format(s.img.bytes_ilegiveis(), ",").replace(",", ".")))
            elif g:
                ok = g.get("MD5") in (None, self.var.md5) and g.get("SHA-1") in (None, self.var.sha1)
                self.log.emit("Integridade: %s com os hashes gravados na aquisição."
                              % ("CONFERE" if ok else "NÃO CONFERE"))
            self.rec = Recuperador(s.img, s.vol, self.var, self.saida, log=self.log.emit,
                                   ao_item=self.item.emit,
                                   ao_etapa=lambda t, f: self.etapa.emit(t))
            self.rec.executar()
            self.fim.emit(self.rec)
        except Exception as e:                       # noqa: BLE001 - mostrar ao perito
            import traceback
            self.log.emit("ERRO: %s\n%s" % (e, traceback.format_exc()))
            self.fim.emit(None)


class Janela(QMainWindow):
    def __init__(self, imagem=None, saida=None, iniciar=False):
        super().__init__()
        self.setWindowTitle("Perícia de mídia — mapa físico e recuperação de vídeo  (v%s)" % VERSAO)
        self.resize(1500, 950)
        self.sessao, self.saida, self.thread, self.trab = None, saida, None, None
        self.t0 = 0
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        self.a_abrir = QAction("Abrir imagem…", self, triggered=self.escolher_imagem)
        self.a_saida = QAction("Pasta de saída…", self, triggered=self.escolher_saida)
        self.a_ini = QAction("▶ Iniciar varredura e recuperação", self, triggered=self.iniciar)
        self.a_parar = QAction("■ Parar", self, triggered=self.parar)
        self.a_png = QAction("Salvar mapa (PNG)", self, triggered=self.salvar_png)
        self.a_pasta = QAction("Abrir pasta de saída", self, triggered=self.abrir_pasta)
        for a in (self.a_abrir, self.a_saida, None, self.a_ini, self.a_parar, None, self.a_png, self.a_pasta):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.mapa = MapaWidget()
        rol = QScrollArea()
        rol.setWidgetResizable(True)
        rol.setWidget(self.mapa)
        self.tabela = QTableWidget(0, 6)
        self.tabela.setHorizontalHeaderLabels(["#", "Arquivo", "Origem", "Duração", "Resolução", "Estado"])
        self.tabela.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tabela.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tabela.verticalHeader().hide()
        self.tabela.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabela.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tabela.cellDoubleClicked.connect(self.abrir_video)
        self.logw = QPlainTextEdit()
        self.logw.setReadOnly(True)
        self.logw.setFont(QFont("Consolas", 9))
        baixo = QSplitter(Qt.Horizontal)
        baixo.addWidget(self.tabela)
        baixo.addWidget(self.logw)
        baixo.setSizes([900, 600])
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(rol)
        sp.addWidget(baixo)
        sp.setSizes([640, 280])
        self.setCentralWidget(sp)
        self.status = QLabel("")
        self.statusBar().addWidget(self.status, 1)
        self._estado(False)
        if imagem:
            self.carregar(imagem)
        if iniciar and self.sessao:
            QTimer.singleShot(600, self.iniciar)

    def _estado(self, rodando):
        self.a_ini.setEnabled(not rodando and self.sessao is not None)
        self.a_parar.setEnabled(rodando)
        self.a_abrir.setEnabled(not rodando)
        self.a_saida.setEnabled(not rodando)

    def log(self, t):
        self.logw.appendPlainText(t)

    def escolher_imagem(self):
        c, _ = QFileDialog.getOpenFileName(self, "Primeiro arquivo da imagem", "",
                                           "Imagens forenses (*.E01 *.e01 *.001 *.dd *.raw *.img *.bin);;Todos (*)")
        if c:
            self.carregar(c)

    def escolher_saida(self):
        d = QFileDialog.getExistingDirectory(self, "Pasta de saída")
        if d:
            self.saida = d
            self.log("Pasta de saída: %s" % d)

    def carregar(self, caminho):
        try:
            self.sessao = Sessao(caminho)
        except Exception as e:                       # noqa: BLE001
            QMessageBox.critical(self, "Imagem", "Não foi possível abrir a imagem:\n%s" % e)
            return
        self.mapa.carregar(self.sessao)
        self.tabela.setRowCount(0)
        self.log("Imagem: %s" % self.sessao.img.caminho)
        self.log("  %s" % self.sessao.img.descricao())
        if self.sessao.img.hashes_gravados:
            self.log("  hashes gravados na aquisição: %s" % self.sessao.img.hashes_gravados)
        if self.sessao.aviso:
            self.log("  " + self.sessao.aviso)
        self.status.setText("Mapa carregado. Saída: %s" % (self.saida or "(escolha ao iniciar)"))
        self._estado(False)

    def _saida_valida(self):
        """A saída nunca fica dentro da pasta da evidência."""
        if not self.saida:
            return False
        pasta_img = os.path.normcase(os.path.dirname(self.sessao.img.caminho))
        alvo = os.path.normcase(os.path.abspath(self.saida))
        if alvo == pasta_img or alvo.startswith(pasta_img + os.sep):
            QMessageBox.warning(self, "Pasta de saída",
                                "A pasta de saída está dentro da pasta da imagem forense.\n"
                                "Escolha outro local, para não misturar material produzido com a evidência.")
            return False
        return True

    def iniciar(self):
        if not self.sessao:
            return
        if not self.saida:
            self.escolher_saida()
        if not self._saida_valida():
            return
        os.makedirs(self.saida, exist_ok=True)
        self.tabela.setRowCount(0)
        self.mapa.recuperados = []
        self.thread = QThread()
        self.trab = Trabalho(self.sessao, self.saida)
        self.trab.moveToThread(self.thread)
        self.thread.started.connect(self.trab.rodar)
        self.trab.progresso.connect(self._progresso)
        self.trab.log.connect(self.log)
        self.trab.item.connect(self._item)
        self.trab.etapa.connect(self._etapa)         # método da janela: executa na thread da interface
        self.trab.fim.connect(self._fim)
        self.t0 = time.time()
        self._estado(True)
        self.thread.start()

    def _etapa(self, t):
        self.status.setText(t)

    def parar(self):
        if self.trab:
            self.trab.parar()
            self.log("Parando…")

    def _progresso(self, v):
        self.mapa.var = v
        self.mapa.cursor_real = float(v.pos)
        resta = (v.total - v.pos) / v.velocidade if v.velocidade else 0
        self.status.setText("Varredura: setor %s de %s · %.1f%% · %.0f MB/s · resta %s" % (
            format(v.pos // 512, ",").replace(",", "."), format(v.total // 512, ",").replace(",", "."),
            100 * v.pos / v.total, v.velocidade / 1e6, mmss(resta)))

    def _item(self, it):
        self.mapa.adicionar_recuperado(it)
        r = self.tabela.rowCount()
        self.tabela.insertRow(r)
        vals = [it["id"], os.path.relpath(it["arquivo"], self.saida), it["origem"], mmss(it.get("duracao")),
                "%sx%s" % (it.get("largura"), it.get("altura")), it["estado"]]
        for c, x in enumerate(vals):
            cell = QTableWidgetItem(str(x))
            cell.setData(Qt.UserRole, it["arquivo"])
            self.tabela.setItem(r, c, cell)

    def _fim(self, rec):
        self.thread.quit()
        self.thread.wait()
        self._estado(False)
        if rec:
            self.mapa.cursor_real = self.mapa.cursor_vis = float(self.sessao.img.tamanho)
            png = os.path.join(self.saida, "mapa_fisico.png")
            self.mapa.grab().save(png)
            self.status.setText("Concluído em %s · %d arquivo(s) · relatório e mapa em %s" % (
                mmss(time.time() - self.t0), len(rec.itens), self.saida))
            self.log("Concluído. Relatório: relatorio.txt · auditoria.jsonl · mapa_fisico.png")

    def abrir_video(self, r, _c):
        arq = self.tabela.item(r, 1).data(Qt.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(arq))

    def abrir_pasta(self):
        if self.saida and os.path.isdir(self.saida):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.saida))

    def salvar_png(self):
        if not self.sessao:
            return
        c, _ = QFileDialog.getSaveFileName(self, "Salvar mapa", os.path.join(self.saida or "", "mapa_fisico.png"),
                                           "PNG (*.png)")
        if c:
            self.mapa.grab().save(c)

    def closeEvent(self, e):
        if self.thread and self.thread.isRunning():
            self.trab.parar()
            self.thread.quit()
            self.thread.wait(5000)
        e.accept()


def abrir_interface(imagem=None, saida=None, iniciar=False):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    j = Janela(imagem, saida, iniciar)
    j.show()
    return app.exec()
