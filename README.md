# Vestigo

Ferramenta de computação forense que abre uma imagem de mídia (E01 ou dd), desenha o **mapa
físico do dispositivo**, acompanha a varredura setor a setor com um cursor animado e **recupera
vídeos e fotos** — vivos, danificados, apagados e sem índice — com relatório, hashes e trilha de
auditoria.

Pensada para perícia: somente leitura sobre a evidência, conferência dos hashes da aquisição,
e cada arquivo gerado com sua origem física (bytes e clusters) e SHA-256.

## Instalação

Requer **Python 3.13** e **FFmpeg**.

```
instalar.bat
```

cria o ambiente em `.venv` e instala as dependências de `requirements.txt` (PySide6, libewf-python,
numpy, PyAV). O FFmpeg é procurado, nesta ordem, na pasta indicada pela variável
`VESTIGO_FFMPEG`, no PATH e na instalação do winget (`Gyan.FFmpeg`).

## Uso

Arraste o **primeiro arquivo da imagem** (`.E01`, `.001`, `.dd`, `.raw`) sobre
`vestigo.bat`, ou rode:

```
vestigo.bat "D:\caso\imagem.E01" --saida "D:\caso\recuperados" --iniciar
```

O mapa físico carrega assim que a imagem abre. **▶ Iniciar** percorre o dispositivo do setor 0
ao último: o cursor mostra a posição da leitura, a faixa "conteúdo lido" se preenche atrás dele
(zeros, 0xFF, dados, trechos ilegíveis) e os arquivos recuperados entram no grupo "Material
recuperado" e na tabela. Duplo clique na tabela abre o arquivo.

Sem janela (servidor, lote):

```
python -m vestigo imagem.E01 --saida pasta --sem-interface
```

A pasta de saída nunca pode ficar dentro da pasta da evidência: o programa recusa.

## O que sai na pasta de saída

| Caminho | Conteúdo |
|---|---|
| `videos/1_vivos` | vídeos presentes e íntegros no sistema de arquivos (cópia lógica) |
| `videos/2_restaurados` | vídeos com `InitializedSize < RealSize` (NTFS): o arquivo no tamanho original, lido dos clusters reais em vez dos zeros que o sistema devolve, e o trecho íntegro cortado no último quadro que está onde o índice diz |
| `videos/3_apagados` | vídeos apagados cuja cadeia de clusters ainda está no sistema de arquivos (registro liberado na `$MFT`; entrada apagada + FAT no exFAT), com o **nome original**, mesmo fragmentados |
| `videos/4_carving` | MP4/MOV completos (`ftyp` + `moov` + `mdat`) contíguos, sem entrada no sistema de arquivos |
| `videos/5_fluxos` | quadros H.264 sem índice (AVCC) remontados e convertidos em MP4, com o **áudio AAC** recuperado das lacunas entre os trechos de vídeo |
| `fotos/1_vivas`, `fotos/2_apagadas` | fotos JPEG (inclui `.insp` de câmeras 360°) vivas e apagadas pela cadeia do sistema de arquivos |
| `fotos/3_carving` | JPEG contíguos sem entrada no sistema de arquivos, cortados no fim da imagem |
| `relatorio.txt` / `relatorio.json` | hashes da imagem (calculados × gravados na aquisição), cada arquivo com origem, faixas físicas, estado e SHA-256, e os apagados **não** recuperados, com o motivo |
| `auditoria.jsonl` | registro de cada etapa e de cada arquivo gerado, com data e hora |
| `mapa_fisico.png` | o mapa ao fim da execução |

Apagados cujo conteúdo está todo em zeros (blocos apagados fisicamente, comum em cartões SD) ou
cujos clusters foram reaproveitados por outro arquivo **não** são exportados como se fossem o
arquivo: ficam listados no relatório com o motivo.

## Garantias

- **Somente leitura.** A imagem é aberta só para leitura; tudo o que o programa produz vai para
  a pasta de saída.
- **Integridade.** A varredura calcula MD5 e SHA-1 do dispositivo inteiro e compara com os
  valores gravados no E01. Trechos ilegíveis da imagem são lidos como zeros, marcados em
  vermelho no mapa e listados no relatório — e nesse caso a integridade nunca é dada como
  conferida.
- **Rastreabilidade.** Cada arquivo gerado tem as faixas físicas de origem e o SHA-256.

## Como funciona

**Sistemas de arquivos.** NTFS (`$MFT`, `$Bitmap`, os três tamanhos de cada arquivo) e exFAT
(diretório com entradas apagadas, FAT e bitmap), em volume superfloppy, MBR ou GPT.

**Vídeos danificados.** Quando a gravação parou antes do fim (`InitializedSize` menor que o
tamanho), o conteúdo real pode continuar nos clusters. O programa lê esses clusters e conta,
pela tabela de amostras do `moov`, quantos quadros estão onde o índice diz.

**Fluxos sem índice.** Quadros H.264 são achados pelo delimitador de quadro em formato AVCC e
remontados; a taxa de quadros vem do SPS (VUI). Sem SPS/PPS próprio, o programa testa os
parâmetros dos outros vídeos do dispositivo e usa o que decodifica.

**Áudio dos fluxos.** As lacunas entre os trechos de vídeo são os blocos de áudio (AAC cru, sem
sincronismo). Cada bloco é dividido em quadros por programação dinâmica, com quatro regras:

1. **grade do CBR** — com reservatório de bits os quadros variam, mas o início do k-ésimo fica
   perto de k × tamanho médio;
2. **cabeçalho AAC-LC** plausível em todo início (elemento, tag, `ics_info`, `max_sfb`);
3. **decodificador** — todo quadro candidato tem de decodificar no AAC do FFmpeg (PyAV);
4. a soma **fecha exatamente** no fim do bloco.

O perfil (tamanho típico, taxa, canais) vem de outro vídeo do dispositivo ou é estimado pelos
próprios bytes. Em 922 blocos de áudio de vídeos reais com índice, a divisão feita **sem o
índice** coincidiu com o índice original em 100% dos blocos.

**Fotos.** Cada JPEG passa por estrutura (marcadores até o EOI), lacuna (4 KiB de bytes iguais
nos dados comprimidos denunciam fragmento), tamanho (mais de 3 bytes por pixel é impossível) e
decodificação. As duas regras do meio existem porque o decodificador sozinho aceita "sem erro"
uma JPEG partida em dois com zeros no meio.

## Testes

```
.venv\Scripts\python -m unittest discover -s tests -t .
```

Os testes geram **dados sintéticos** na hora — JPEGs codificados pelo próprio FFmpeg e uma
imagem dd montada em memória — e conferem: hashes da varredura, recuperação byte a byte de
fotos contíguas, recusa de foto fragmentada, trailer após o fim da imagem e SHA-256 no
relatório. Nenhum dado real acompanha o repositório.

## Limites desta versão

- Outros sistemas de arquivos (FAT32, ext4, APFS): só carving e busca de fluxos.
- Carving de MP4 e JPEG apenas **contíguo**; pela cadeia do sistema de arquivos, fragmentados
  saem normalmente.
- Fluxos sem índice: H.264 AVCC com delimitador de quadro. HEVC e MPEG-TS ficam para depois
  (arquivos HEVC com índice são recuperados normalmente).
- Áudio de fluxos: só AAC **CBR**. Áudio VBR não tem divisão única sem o índice; o programa
  declara `sem-audio` e o motivo, em vez de adivinhar.
- Fotos: só JPEG.
- Nomes de arquivos apagados só aparecem se a entrada ainda existir (a leitura do `$LogFile`
  do NTFS ainda não está no programa).

## Uso em perícia

A ferramenta automatiza técnicas consolidadas da computação forense, mas **a responsabilidade
pelo exame é de quem o assina**. Valide a versão que usar contra casos com resposta conhecida
(ISO/IEC 27041) e registre no laudo a versão e o relatório gerado.

## Licença

Copyright (C) 2026 Ralph Bittencourt

Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo sob os termos da
**GNU General Public License**, versão 3 ou (a seu critério) qualquer versão posterior,
publicada pela Free Software Foundation. É distribuído na esperança de ser útil, mas **sem
nenhuma garantia**, nem mesmo a garantia implícita de comercialização ou de adequação a um
propósito específico. Veja o arquivo [`LICENSE`](LICENSE).

Para uso sob outros termos (por exemplo, incorporar a um produto de código fechado), entre em
contato com o autor sobre licenciamento comercial.
