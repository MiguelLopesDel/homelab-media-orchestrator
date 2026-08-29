# Arquitetura do setup

Este mapa registra o estado desejado. Setas representam fluxo de dados; caixas
com linha pontilhada são ferramentas operadas manualmente, fora do caminho
automático. Nenhum módulo pode apagar mídia que não criou.

## Aquisição de mídia

```text
┌──────────────┐ pedido  ┌───────────────────────────────┐
│ Jellyseerr   ├────────>│ Sonarr (séries) / Radarr      │
└──────────────┘         │ (filmes; auditoria somente)   │
                         └──────┬───────────────┬────────┘
                                │ busca          │ decisão/importação
                                v                v
                    ┌───────────────────┐   ┌────────────────────────┐
                    │ Prowlarr/indexers │   │ acquisition module     │
                    └─────────┬─────────┘   │ observer + dispatcher  │
                              │ resultados     │ reconciler + cache   │
                              v                └──────────┬─────────┘
                    ┌───────────────────┐                 │
                    │ candidate vault   │<────────────────┘
                    │ URL/hash/metadado │
                    └─────────┬─────────┘
                              v
                    ┌───────────────────┐
                    │ qBittorrent       │
                    │ torrents/anime    │
                    │ incompletos/anime │
                    └─────────┬─────────┘
                              │ importação por hardlink
                              v
             ┌────────────────────────────────────────────┐
             │ library/anime e library/filmes             │
             │ vídeo + áudio/legendas externas            │
             └──────┬──────────┬───────────┬──────────────┘
                    │          │           │
                    v          v           v
                Jellyfin     Shoko       Bazarr
                    │      identifica    procura PT-BR
                    v
             TV / navegador / apps
```

O módulo `automation/acquisition` mantém a lógica de decisão num único lugar. Integrações HTTP e bancos são adaptadores internos; chamadas externas não devem espalhar regras por scripts novos.

O `candidate vault` registra resultados já consultados para reduzir chamadas aos
indexadores. URLs privadas podem ser guardadas apenas no estado privado do
servidor; Git recebe somente código, formatos e documentação, nunca passkeys.

## Idiomas por item

```text
arquivo do episódio
    |
    +-- áudio PT-BR presente ------------------> concluído
    |
    +-- dublagem exata confirmada, mas ausente -> alerta de upgrade dublado
    |
    +-- sem dublagem confirmada
          |
          +-- legenda PT-BR presente ----------> concluído
          +-- Bazarr/indexadores de legenda
          +-- tradução de legenda textual
          +-- OCR + tradução (último recurso; notebook)
```

```text
fonte com legenda PT-BR ─┐
                         v
                  . . . . . . . . . . . . . . . .
                  . subtitle migration module       .
                  . inspect -> extract -> verify    .
                  .       \-> shift/retime -> verify.
                  . . . . . . . . . . . . . . . .
                                 │ sidecar atômico
                                 v
                         biblioteca / Jellyfin
```

O módulo de migração é deliberadamente manual no ponto de decisão: duração
parecida é evidência, não prova de sincronização. Cortes, bumpers e aberturas são
descritos num manifesto revisável, nunca inferidos e aplicados silenciosamente.

```text
Sonarr + Radarr + ffprobe + sidecars
                │
                v
      resolver de disponibilidade
      (TVDB -> AnimeBridge -> MAL/AniList -> fonte oficial cacheada)
                │ prova exata ou desconhecida
                v
      subtitle orchestrator ──> estado por episódio/filme
                │                       │
                │                       ├── dublagem existe e falta -> Discord
                │                       ├── sem dublagem e sem PT-BR -> Discord/job
                │                       └── requisito atendido       -> concluído
                v
      Bazarr -> tradução textual -> OCR/tradução no notebook
```

O scan é por episódio, não apenas por série ou temporada. Filme usa o mesmo
critério, mas continua sem ações automáticas de aquisição nesta fase.

### Dublagem externa

```text
missing_dub por episódio
          │
          v
┌──────────────────────────┐      cache primeiro; busca exata <= 1/6 h
│ dub orchestrator         ├──────────────────────────────────────────┐
└────────────┬─────────────┘                                          │
             v                                                        v
   qBittorrent dub-source <──────────────────────────── candidate vault/Sonarr
             │ fonte completa, sem importação
             v
   ffprobe(audio por) -> fingerprint visual rápido
                              │ duração/cortes diferentes
                              v
                     timeline alignment (1 CPU)
                     offsets + lacunas sem diálogo
                              │
                              v
                     external audio builder
                                                   │
                                                   v
                                    vídeo original + .por.default.m4a
```

O seam de publicação é o sidecar: o dublador não chama importação do Sonarr e
não reescreve o contêiner. Casos sem prova forte param para manifesto revisado.

A disponibilidade de dublagem é uma propriedade canônica por série, temporada
e episódio. Em temporada concluída, um catálogo mapeado que cobre todos os
episódios regulares determina PT-BR para a temporada toda; em lançamento a
resolução continua por episódio. S00, OVAs e filmes não herdam esse estado.
Catálogos amplos sem mapeamento exato servem só para descoberta.

`dublagem.json` é um override documentado, não uma lista a ser mantida para
cada anime. A resolução normal começa pelo ID TVDB do Sonarr e usa os
identificadores AnimeBridge/MAL/AniList para localizar a fonte oficial uma vez,
com cache. Em temporada já encerrada, só uma enumeração oficial de todos os
episódios permite declarar a temporada inteira; em lançamento, a evidência é
sempre episódica.

Filmes seguem a mesma regra de conclusão. O auditor consulta o Radarr, examina
o arquivo e seus sidecars e cruza o TMDB exato com o cache AnimeBridge +
MyDubList. Filmes ficam em tabelas próprias para que IDs do Radarr nunca
colidam com IDs de episódios do Sonarr. Nesta fase, filmes são somente
auditados e alertados: nenhuma busca, substituição ou tradução é disparada pelo
auditor.

## Estado e código

- Código/declarativos: Git local.
- Segredos: `.env` e configs privadas, nunca Git.
- Estado de aplicativos: bancos e diretórios em `/srv/homelab/config`.
- Mídia: volumes montados sob `/srv/media`.
- Recuperação: snapshot privado em armazenamento secundário e espelho Git bare
  independente, fora da árvore de trabalho.

## Storage e ciclo de vida

```text
discos membros ──mergerfs──> /srv/media
                               ├── torrents/      payload para seed
                               ├── library/       hardlinks + sidecars
                               ├── .lixeira/      versões substituídas pelo Sonarr
                               └── backups/       restauração fora do Git
```

Ramos reservados a backup entram como `NC`: aparecem no pool, mas a política de
criação não envia mídia nova para esses discos.

- `torrents/` e `library/` precisam permanecer no mesmo filesystem para que a
  importação use hardlinks.
- Um `Upgrade` do Sonarr pode mandar a versão anterior para `.lixeira`. Isso não
  atualiza o caminho esperado pelo qBittorrent; antes de limpar ou restaurar,
  conferir inode, faixas e histórico.
- Sidecars são preferidos quando alterar o contêiner quebraria o hash do torrent.
- O Git guarda configuração e ferramentas; bancos vivos, mídia, cache e segredos
  permanecem fora dele.
