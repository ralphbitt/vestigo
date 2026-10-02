# Vestigo — Copyright (C) 2026 Ralph Bittencourt
# SPDX-License-Identifier: GPL-3.0-or-later
"""Leitura do NTFS direto da imagem: geometria ($Boot), $MFT, $Bitmap e os três tamanhos de
cada arquivo (AllocatedSize, RealSize e InitializedSize).

Somente leitura.
"""
import datetime
import struct

import numpy as np

EXT_VIDEO = (".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mts", ".m2ts", ".mkv", ".wmv", ".mpg",
             ".mpeg", ".ts", ".vob", ".webm", ".flv", ".insv", ".lrv")


# ---------------------------------------------------------------------- utilidades
def filetime(ft):
    if not ft:
        return None
    try:
        return (datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)).isoformat(
            sep=" ", timespec="seconds") + " UTC"
    except OverflowError:
        return None


def _fixups(rec, setor=512):
    usa_off, usa_cnt = struct.unpack_from("<HH", rec, 0x04)
    if usa_cnt == 0 or usa_off + usa_cnt * 2 > len(rec):
        return rec
    usn = rec[usa_off:usa_off + 2]
    out = bytearray(rec)
    for i in range(1, usa_cnt):
        pos = i * setor - 2
        if pos + 2 > len(out) or bytes(out[pos:pos + 2]) != usn:
            break
        out[pos:pos + 2] = rec[usa_off + i * 2:usa_off + i * 2 + 2]
    return bytes(out)


def decodifica_runs(buf, p, fim):
    """Data runs -> [(lcn | None, n_clusters)]; None = trecho esparso."""
    runs, lcn = [], 0
    while p < fim:
        h = buf[p]
        if h == 0:
            break
        lf, of = h & 0x0F, h >> 4
        p += 1
        if lf == 0 or p + lf + of > fim:
            break
        n = int.from_bytes(buf[p:p + lf], "little")
        p += lf
        if of == 0:
            runs.append((None, n))
            continue
        lcn += int.from_bytes(buf[p:p + of], "little", signed=True)
        p += of
        runs.append((lcn, n))
    return runs


def parse_registro(rec):
    if rec[:4] != b"FILE":
        return None
    rec = _fixups(rec)
    flags = struct.unpack_from("<H", rec, 0x16)[0]
    p = struct.unpack_from("<H", rec, 0x14)[0]
    lim = min(struct.unpack_from("<I", rec, 0x18)[0], len(rec))
    info = {"em_uso": bool(flags & 1), "dir": bool(flags & 2),
            "base": struct.unpack_from("<Q", rec, 0x20)[0] & 0xFFFFFFFFFFFF,
            "nomes": [], "si": None, "dados": [], "indice": []}
    while p + 8 <= lim:
        tipo, tam = struct.unpack_from("<II", rec, p)
        if tipo == 0xFFFFFFFF or tam == 0 or p + tam > len(rec):
            break
        nres = rec[p + 8]
        nlen, noff = rec[p + 9], struct.unpack_from("<H", rec, p + 0x0A)[0]
        anome = rec[p + noff:p + noff + nlen * 2].decode("utf-16-le", "replace") if nlen else ""
        if tipo == 0x10 and not nres:
            v = p + struct.unpack_from("<H", rec, p + 0x14)[0]
            info["si"] = struct.unpack_from("<QQQQ", rec, v)
        elif tipo == 0x30 and not nres:
            v = p + struct.unpack_from("<H", rec, p + 0x14)[0]
            pai = struct.unpack_from("<Q", rec, v)[0] & 0xFFFFFFFFFFFF
            ln, ns = rec[v + 0x40], rec[v + 0x41]
            nome = rec[v + 0x42:v + 0x42 + ln * 2].decode("utf-16-le", "replace")
            info["nomes"].append((nome, ns, pai))
        elif tipo in (0x80, 0xA0):
            d = {"nome": anome, "nres": bool(nres)}
            if nres:
                svcn = struct.unpack_from("<Q", rec, p + 0x10)[0]
                roff = struct.unpack_from("<H", rec, p + 0x20)[0]
                alloc, real, init = struct.unpack_from("<QQQ", rec, p + 0x28)
                d.update(svcn=svcn, alloc=alloc, real=real, init=init,
                         runs=decodifica_runs(rec, p + roff, p + tam))
            else:
                vlen = struct.unpack_from("<I", rec, p + 0x10)[0]
                d.update(svcn=0, alloc=0, real=vlen, init=vlen, runs=[])
            info["dados" if tipo == 0x80 else "indice"].append(d)
        p += tam
    return info


class Arquivo:
    """Um registro da $MFT com seu fluxo $DATA principal."""

    def __init__(self, num, reg):
        self.num = num
        self.em_uso = reg["em_uso"]
        self.dir = reg["dir"]
        longos = [n for n in reg["nomes"] if n[1] != 2]
        n = (longos or reg["nomes"] or [("", 0, 0)])[0]
        self.nome, self.pai = n[0], n[2]
        self.caminho = self.nome
        si = reg["si"] or (0, 0, 0, 0)
        self.criado, self.modificado = filetime(si[0]), filetime(si[1])
        principal = [d for d in reg["dados"] if d["nome"] == "" and d["nres"]]
        d = principal[0] if principal else None
        self.real = d["real"] if d else 0
        self.alloc = d["alloc"] if d else 0
        self.init = d["init"] if d else 0
        self.runs = list(d["runs"]) if d else []
        self.indice_runs = [r for x in reg["indice"] if x["nres"] for r in x["runs"]]
        self.ads = [x["nome"] for x in reg["dados"] if x["nome"]]

    @property
    def metadado(self):
        return self.num < 24 or self.nome.startswith("$")

    @property
    def video(self):
        return self.nome.lower().endswith(EXT_VIDEO)

    @property
    def danificado(self):
        return self.real > 0 and self.init < self.real

    def extents(self, cluster):
        """Trechos físicos [(offset_bytes_no_volume, n_bytes, vcn_inicial)]."""
        out, vcn = [], 0
        for lcn, n in self.runs:
            if lcn is not None:
                out.append((lcn * cluster, n * cluster, vcn))
            vcn += n
        return out


class VolumeNTFS:
    tipo = "NTFS"

    def __init__(self, img, offset=0):
        self.img, self.offset = img, offset
        self.inicio = offset
        b = img.ler(offset, 512)
        if b[3:11] != b"NTFS    ":
            raise ValueError("não há NTFS no offset %d" % offset)
        self.bps = struct.unpack_from("<H", b, 0x0B)[0]
        self.spc = b[0x0D]
        self.cluster = self.bps * self.spc
        self.setores = struct.unpack_from("<Q", b, 0x28)[0]
        self.tamanho = (self.setores + 1) * self.bps
        self.n_clusters = self.setores * self.bps // self.cluster
        self.mft_lcn = struct.unpack_from("<Q", b, 0x30)[0]
        cpr = struct.unpack_from("<b", b, 0x40)[0]
        self.reg_tam = (1 << -cpr) if cpr < 0 else cpr * self.cluster
        self.arquivos = {}
        self.bitmap = None
        self._ler_mft()
        self._ler_bitmap()

    def ler_runs(self, runs, limite=None):
        partes, total = [], 0
        for lcn, n in runs:
            nb = n * self.cluster
            partes.append(b"\x00" * nb if lcn is None else
                          self.img.ler(self.offset + lcn * self.cluster, nb))
            total += nb
            if limite and total >= limite:
                break
        dado = b"".join(partes)
        return dado[:limite] if limite else dado

    def _ler_mft(self):
        r0 = self.img.ler(self.offset + self.mft_lcn * self.cluster, self.reg_tam)
        reg0 = parse_registro(r0)
        d = [x for x in reg0["dados"] if x["nome"] == "" and x["nres"]][0]
        mft = self.ler_runs(d["runs"], d["real"])
        regs = {}
        for i in range(len(mft) // self.reg_tam):
            r = parse_registro(mft[i * self.reg_tam:(i + 1) * self.reg_tam])
            if r:
                regs[i] = r
        # registros de extensão (atributos que não couberam no registro base)
        for i, r in list(regs.items()):
            if r["base"] and r["base"] in regs:
                b = regs[r["base"]]
                for x in r["dados"]:
                    alvo = [y for y in b["dados"] if y["nome"] == x["nome"] and y["nres"]]
                    if alvo and x.get("svcn", 0) > 0:
                        alvo[0]["runs"] = alvo[0]["runs"] + x["runs"]
                del regs[i]
        for i, r in regs.items():
            self.arquivos[i] = Arquivo(i, r)
        for a in self.arquivos.values():
            partes, p, vistos = [a.nome], a.pai, set()
            while p in self.arquivos and p != 5 and p not in vistos:
                vistos.add(p)
                partes.append(self.arquivos[p].nome)
                p = self.arquivos[p].pai
            a.caminho = "\\".join(reversed(partes))

    def _ler_bitmap(self):
        a = self.arquivos.get(6)
        if not a or not a.runs:
            return
        bits = np.frombuffer(self.ler_runs(a.runs, a.real), dtype=np.uint8)
        self.bitmap = np.unpackbits(bits, bitorder="little")[:self.n_clusters].astype(bool)

    def livres(self):
        """Faixas livres segundo o $Bitmap: [(offset_bytes_no_volume, n_bytes)]."""
        if self.bitmap is None:
            return []
        b = np.concatenate(([True], self.bitmap, [True]))
        d = np.diff(b.astype(np.int8))
        ini = np.nonzero(d == -1)[0]
        fim = np.nonzero(d == 1)[0]
        return [(int(i) * self.cluster, int(f - i) * self.cluster) for i, f in zip(ini, fim)]


# ---------------------------------------------------------------------- volumes
SISTEMAS = {b"NTFS    ": "NTFS", b"EXFAT   ": "exFAT"}


def localizar_volumes(img):
    """Volumes NTFS e exFAT na imagem: superfloppy, MBR ou GPT. Devolve [(offset, tipo)]."""
    def tipo_em(off):
        return SISTEMAS.get(img.ler(off + 3, 8))

    if tipo_em(0):
        return [(0, tipo_em(0))]
    achados = []
    s0 = img.ler(0, 512)
    if s0[510:512] == b"\x55\xaa":
        for k in range(4):
            e = s0[0x1BE + k * 16:0x1BE + (k + 1) * 16]
            tipo, lba = e[4], struct.unpack_from("<I", e, 8)[0]
            if tipo == 0xEE:
                h = img.ler(512, 512)
                if h[:8] == b"EFI PART":
                    ent_lba, n_ent, tam_ent = struct.unpack_from("<QII", h, 0x48)
                    tab = img.ler(ent_lba * 512, n_ent * tam_ent)
                    for j in range(n_ent):
                        ini = struct.unpack_from("<Q", tab, j * tam_ent + 0x20)[0]
                        if ini and tipo_em(ini * 512):
                            achados.append((ini * 512, tipo_em(ini * 512)))
            elif tipo and lba and tipo_em(lba * 512):
                achados.append((lba * 512, tipo_em(lba * 512)))
    return achados
