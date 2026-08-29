# Áudio externo sincronizado

O Jellyfin aceita uma faixa de áudio ao lado do vídeo quando o nome começa
com o nome exato do vídeo e termina, por exemplo, em `.por.default.m4a`.
Isso permite melhorar ou adicionar uma dublagem sem reescrever o vídeo e sem
invalidar o hardlink/hash usado pelo qBittorrent.

`automation/subtitles/external_audio_builder.py` materializa essas faixas a
partir de um manifesto de edição revisado. Cada segmento aponta para o áudio
dublado (`source`) ou para o áudio original do vídeo (`target`). Segmentos do
target são usados apenas em bumpers, eyecatches ou trechos ausentes na edição
dublada.

O renderizador:

- usa um thread, prioridade de CPU baixa e I/O ocioso;
- nunca modifica o vídeo;
- não sobrescreve sidecars sem `--replace`;
- escreve primeiro em `.partial.m4a` e publica por renomeação atômica;
- valida duração e tag `por` antes de publicar;
- confere o tamanho original do vídeo em `verify`.
- aceita `source_audio_index`/`target_audio_index`, relativos apenas às faixas de
  áudio, para não assumir que português é sempre a primeira faixa;
- quando `require_source_language` está presente, recusa uma fonte cuja faixa
  selecionada não esteja identificada como português.

Uma faixa única sem tag de idioma só entra automaticamente quando o
orquestrador registrou evidência forte de dublagem no release. No uso manual,
prefira sempre `require_source_language` e uma faixa corretamente etiquetada.

Quando as durações divergem, `timeline_alignment.py` pode gerar os mesmos
segmentos automaticamente. Ele alinha fingerprints visuais por blocos e só
preenche uma lacuna com áudio do alvo quando uma legenda textual comprova que
o intervalo não contém diálogo. O manifesto manual continua sendo a saída para
casos ambíguos.

Exemplo:

```bash
python3 automation/subtitles/external_audio_builder.py \
  render config/media/example-season-external-dub.json --episode 1
python3 automation/subtitles/external_audio_builder.py \
  verify config/media/example-season-external-dub.json
```
