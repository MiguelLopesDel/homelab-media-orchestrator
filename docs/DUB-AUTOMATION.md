# Dublagem externa automática

O módulo de dublagem resolve episódios e filmes que o auditor marcou como
`missing_dub`: o catálogo determinou que a dublagem existe, mas o arquivo atual
não possui áudio PT-BR. Episódios usam `dub_jobs`; filmes usam `movie_dub_jobs`, de
modo que um ID do Radarr nunca colida com um ID do Sonarr.

```text
subtitle auditor -> dub job -> cache local -> staging qBittorrent
                                      \-> busca exata limitada (último recurso)
staging -> prova de áudio por -> prova rápida de timeline -> sidecar -> Jellyfin
                                      \-> alinhamento por blocos (se divergir)
```

O vídeo da biblioteca é entrada somente-leitura. A fonte dublada fica na
categoria `dub-source`, em `/data/torrents/dub-staging`, e pode continuar
semeando. Ela nunca é entregue ao Sonarr para importação, portanto não substitui
o vídeo principal nem quebra seu hardlink/hash.

## Interface

```bash
bin/homelab dub scan
bin/homelab dub status
bin/homelab dub step                    # preview
bin/homelab dub step --apply            # só cache; não consulta indexer
bin/homelab dub step --apply --allow-search
bin/homelab dub step --apply --episode-id 123  # sonda controlada de um episódio

bin/homelab dub-audio analyse SOURCE TARGET
bin/homelab dub-audio publish SOURCE TARGET
```

O cron de produção executa `scan` e avança até quatro **temporadas distintas** a
cada 15 minutos. Mesmo com `--allow-search`, o módulo usa candidatos já
registrados primeiro e permite no máximo uma busca exata via Sonarr a cada seis
horas **por temporada**. A busca não faz grab pelo Sonarr; o release escolhido
vai diretamente ao staging.

## Máquina de estados

```text
queued
  -> candidate_selected
  -> metadata_wait
  -> downloading
  -> analysing
  -> ready
  -> published

saídas conservadoras:
  waiting_candidate  nenhum candidato elegível para sonda no cache/budget
  needs_review       arquivo ambíguo, corte ou timeline diferente
  failed             erro explícito, sem loop destrutivo
  fulfilled          outro processo já resolveu o episódio
```

Cada execução avança no máximo um estado de um episódio. O estado fica nas
tabelas `dub_jobs`, `dub_events` e `dub_budgets`, dentro do banco privado do
auditor. Queda de energia não perde o trabalho.

## Portões de segurança

1. O episódio precisa estar `missing_dub`; legenda não dispara dublagem.
2. A dublagem já foi provada pelo catálogo antes da busca. Candidatos com marca
   forte (`Dublado PT-BR`, idioma português explícito, Anipakku/IceBlue etc.)
   têm prioridade.
   `MULTi`/`DUAL` sem idioma explícito também pode baixar **um único episódio**
   para inspeção real das faixas. `English Dub`, francês e `Multi-Subs PT-BR`
   não passam nem como sonda.
3. O arquivo baixado precisa conter exatamente uma faixa identificável como
   português. A exceção conservadora é uma única faixa marcada `und`: ela só
   pode ser aceita quando o release já passou pelo marcador forte de dublagem;
   essa origem fica registrada como evidência, sem fingir que veio do `ffprobe`.
4. O membro do torrent precisa corresponder sem ambiguidade ao SxxExx/absoluto.
   Em filmes, um pack misto só passa quando exatamente um vídeo é marcado como
   `Movie`, `Film`, `Filme` ou `Gekijouban`; extras ambíguos param para revisão.
5. Diferença de até 150 ms usa a prova rápida. Acima disso, o alinhador denso
   procura offsets constantes por blocos e materializa as inserções/cortes.
6. O alinhador denso usa quatro fingerprints por segundo, um único núcleo e
   thread, prioridade baixa e limite de 20 minutos por arquivo. Host ocupado
   adia o job, sem transformá-lo em falha. O vetor é cacheado por caminho,
   tamanho e `mtime`, portanto uma repetição não decodifica o mesmo arquivo.
7. Fingerprints visuais alinhados precisam cobrir pelo menos 85% da edição e
   coincidir em pelo menos 90%, com distância mediana baixa.
8. Lacuna exclusiva do alvo só pode conservar o áudio original quando uma
   legenda textual PT/EN prova que não existe diálogo naquele intervalo. Se
   houver fala ou faltar essa prova, o episódio segue para revisão.
   Uma cauda de encode de até um segundo é tratada separadamente como borda.
9. O vídeo alvo precisa manter o mesmo tamanho até o fim do render.
10. A saída é escrita como `.partial.m4a`, validada, e publicada por renomeação
   atômica como `.por.default.m4a`. Saída existente não é sobrescrita.

Se os portões visuais ou de diálogo falharem, o módulo não tenta “adivinhar”. O caso segue para
`needs_review` e usa o manifesto do `external_audio_builder.py`, documentado em
`EXTERNAL-AUDIO.md`, para mapear cortes manualmente.

## Torrent já existente

Se o mesmo hash já estiver completo em outra categoria, a fonte é adotada em
modo somente-leitura. O módulo não muda prioridade, categoria ou estado desse
torrent. Torrent incompleto fora de `dub-source` exige revisão.

## Limites conhecidos

- Áudio sem tag em release sem evidência forte, ou com mais de uma faixa
  ambígua, é recusado por segurança.
- Packs cuja numeração não produz um único arquivo exato exigem revisão.
- Cortes comprovados e sem diálogo nas lacunas geram manifesto automaticamente;
  casos ambíguos ainda precisam de manifesto revisado.
- Um filme sem candidato com marca forte de áudio PT-BR fica em
  `waiting_candidate`. Para episódios cuja dublagem já foi confirmada, um
  `Dual-Audio`/`MULTi` ambíguo pode ser testado em um único arquivo; só uma
  faixa realmente identificada como português libera o restante do pack.

## Disponibilidade de dublagem: resolução determinística, override manual

O arquivo `dublagem.json` **não é uma lista de trabalho manual**. Ele existe
somente para overrides pequenos e auditáveis: uma correção de fonte, uma
temporada com lançamento irregular ou uma prova histórica que as fontes
automáticas ainda não publicaram.

A descoberta normal deve acontecer, com cache e sem uma consulta por episódio,
nesta ordem. O resultado é uma propriedade objetiva de cada episódio: há
dublagem PT-BR, não há, ou o episódio ainda não foi disponibilizado. `unknown`
é falha de adaptador/mapeamento, nunca uma conclusão sobre a obra.

```text
Sonarr (TVDB + temporada/episódio)
  -> AnimeBridge (TVDB <-> MAL/AniList; mapeamento de episódio)
  -> MyDubList em lote (sinal de disponibilidade)
  -> AniList (link oficial Crunchyroll, cacheado por série)
  -> metadados oficiais da Crunchyroll por temporada/episódio
  -> evidência persistida: fonte, data, série, temporada e episódio
```

Na implementação atual, MyDubList `high` e `normal` entram como dados de
disponibilidade em lote, mas **só** depois que AnimeBridge resolve TVDB e MAL.
Em uma temporada concluída, quando o mapeamento cobre todos os episódios
regulares, a disponibilidade PT-BR é herdada pela temporada inteira. Em obra em
lançamento, a regra continua episódica. Especiais (S00), OVAs e filmes nunca
herdam a temporada principal. A publicação continua dependendo de encontrar um
release com marcador forte e verificar a faixa real `por` com `ffprobe`.

O último adaptador deve usar somente metadados públicos/permitidos e cachear a
resposta por série. Ele **não** baixa vídeo, não contorna DRM e não consulta o
catálogo inteiro a cada scan. Para uma temporada concluída, uma resposta oficial
que enumere a faixa PT-BR de todos os episódios é suficiente; para uma temporada
em lançamento, registra apenas os episódios já enumerados. Falha de fonte é
`desconhecida`, nunca “não há dublagem”.

Assim, adicionar uma série nova não exige que alguém a inclua no JSON. O JSON
só prevalece sobre a descoberta automática quando contém uma fonte e data de
verificação; toda exceção deve ser promovida ao resolver automático quando o
mesmo caso reaparecer.
