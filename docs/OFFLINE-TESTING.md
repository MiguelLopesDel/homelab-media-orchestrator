# Testes offline

O repositório é testável sem NAS, rede, Docker, Sonarr, qBittorrent, Prowlarr,
indexadores ou credenciais:

```bash
bin/homelab verify
```

Além dos testes unitários, `tests/test_offline_dub_flow.py` cria em diretório
temporário um MKV de um segundo, 720p, com áudio japonês e PT-BR. Ele percorre
o caminho real de `ffprobe -> validação -> step_episode -> hardlink` e confirma
que uma fonte dublada de qualidade não é transcodificada como áudio externo.

O fixture é criado no momento do teste e é removido automaticamente. Nenhum
arquivo de mídia real, serviço local ou chamada HTTP é usado.

Para executar apenas essa regressão:

```bash
PYTHONPATH=automation/subtitles:automation/acquisition \
  python3 -m unittest discover -s tests -p test_offline_dub_flow.py
```
