# Migração e sincronização de legendas

`automation/subtitles/subtitle_migration.py` concentra o procedimento aprendido
durante as migrações de releases. Sua interface operacional é:

```bash
bin/homelab subtitles inspect SOURCE.mkv TARGET.mkv --language por
bin/homelab subtitles extract SOURCE.mkv TARGET.mkv --language por
bin/homelab subtitles shift SOURCE.ass OUTPUT.ass --offset 1.250
bin/homelab subtitles retime config/media/meu-ajuste.json
bin/homelab subtitles verify TARGET.pt-BR.ass TARGET.mkv
```

O vídeo é sempre entrada somente-leitura. `extract`, `shift` e `retime` publicam
primeiro um arquivo parcial e só o renomeiam depois da validação; não
sobrescrevem uma saída sem `--replace`.

## Fluxo de decisão

1. Execute `inspect`. Ele localiza uma faixa textual PT-BR, compara início e
   duração das edições e informa o índice/codec da faixa.
2. Diferença absoluta até 250 ms é `likely-compatible`. Isso permite extrair,
   mas ainda exige conferir uma fala no início, uma no meio e uma no fim.
3. Execute `extract`; o nome automático é o nome exato do vídeo seguido de
   `.pt-BR.ass` ou `.pt-BR.srt`, formato reconhecido pelo Jellyfin.
4. Execute `verify`. Ele valida sintaxe, quantidade de falas e se a timeline
   ultrapassa o fim do vídeo.
5. Offset constante: use `shift --offset`, em segundos; valor positivo atrasa a
   legenda, negativo adianta.
6. Corte, bumper ou abertura diferente: descreva os trechos preservados num
   manifesto e use `retime`. Não tente corrigir diferença estrutural com um único
   offset.

`inspect` não afirma que duas edições estão sincronizadas só porque têm a mesma
duração. Essa limitação é intencional: detectar semanticamente fala contra áudio
exige uma ferramenta de alinhamento acústico e revisão humana.

## Manifesto de cortes

Veja `config/media/subtitle-retime.example.json`. Cada segmento significa:

```text
[source_start, source_end] da legenda de origem
              -> começa em target_start na edição destino
```

Exemplo: a origem tem um bumper de cinco segundos entre 92,5 e 97,5, ausente no
destino. O primeiro segmento preserva 0–92,5; o segundo começa em 97,5 da origem
e passa a começar em 92,5 no destino. Falas que cruzam um corte são recortadas;
falas inteiramente dentro do trecho removido são descartadas.

## Limites e rotas alternativas

- O alinhador trabalha com ASS textual. Extração também aceita SRT, mas `shift`,
  `retime` e `verify` exigem ASS nesta versão.
- PGS/VobSub são imagens. O módulo recusa essas faixas: elas devem seguir pelo
  fluxo OCR no notebook e só depois retornar como ASS/SRT textual.
- Tradução por API continua sendo o último recurso, porque consome quota e pode
  depender do notebook.
- Para adicionar dublagem sem invalidar o torrent, use
  `external_audio_builder.py` e consulte `docs/EXTERNAL-AUDIO.md`.

## Caso de referência: duas edições do mesmo episódio

- Fonte: 1740,032 s, ASS PT-BR, 435 falas.
- Destino: 1739,947 s.
- Delta: 85 ms; primeira fala em 2,44 s e última em 1738,83 s.

O sidecar foi extraído diretamente, sem retime. Esse caso vira teste operacional
do fluxo, não uma regra para declarar qualquer par com duração próxima como
sincronizado.

## Testes

`tests/test_subtitle_migration.py` cobre parsing ASS, offset constante,
manifesto com corte, rejeição de segmentos sobrepostos e falas além do vídeo.
Tudo roda junto com:

```bash
bin/homelab verify
```
