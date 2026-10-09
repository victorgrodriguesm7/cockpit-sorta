# Cockpit Sorta

Página do Cockpit para encontrar vídeos pendentes e catalogar filmes e séries no formato do [Sorta](https://github.com/victorgrodriguesm7/sorta). Cada pasta cadastrada é um catálogo independente: contém seu próprio `sorta.db`, `manifest.json`, `poster/`, filmes e séries. É possível cadastrar pastas de outros discos quando forem montados no servidor.

## Estado da primeira versão

- Lista os discos de dados montados, cadastra uma pasta existente em cada disco e guarda seu UUID. Antes de varrer ou gravar, confirma que o mesmo disco continua montado. A configuração fica em `~/.config/cockpit-sorta/config.json` do usuário conectado ao Cockpit.
- Varrer é somente leitura. Detecta vídeos pendentes recursivamente, ignora cache, arquivos temporários e lixo do sistema, e compara com o banco. Em bancos v4, cada episódio é identificado pelo `episodes.file_path`.
- Busca filmes e séries no TMDB, mostra o destino e os arquivos associados (legendas e `.nfo`) antes de mover. Séries aceitam vários episódios em ordem, temporada e número inicial; a renomeação é opcional.
- Grava o esquema v4 de `sorta.db`, metadados, gêneros, episódios, pôster principal quando disponível e `manifest.json`. Um banco existente de versão diferente precisa ser migrado primeiro pelo Sorta desktop.
- Salva cópia SQLite do banco existente no SSD do usuário antes de organizar. Uma operação interrompida gera aviso e pode ser recuperada pela tela.
- A aba **Catálogo** lista filmes e séries do `sorta.db`, mostra primeiro o pôster local indicado por `poster_path` e usa o endereço TMDB salvo no banco quando o arquivo local não está disponível.
- Permite adicionar ou remover gêneros de uma mídia e escolher o principal. O principal aparece primeiro; os demais são exibidos alfabeticamente no Cockpit. O banco grava somente `is_primary`, como no Sorta desktop.
- Em **Discos, TMDB e traduções**, permite renomear gêneros conhecidos e o prefixo da pasta de temporada para o catálogo selecionado. Mudanças que afetam pastas existentes mostram uma prévia, criam backup do banco, movem as pastas e atualizam os caminhos no SQLite. Uma edição interrompida pode ser recuperada na tela.

Os arquivos SQL foram copiados sem mudanças de `sorta` na revisão `56939029402b734a268a2ec718dca1aea228b4dc` (migrações 0001–0004). O documento `docs/disk-format.md` desse commit ainda descreve o esquema v3, mas o código e a migração 0004 usam a versão 4 e incluem episódios, `catalogued_at` e `is_new`. O helper usa a versão 4 e registra os checksums SHA-384 na tabela `_sqlx_migrations` para compatibilidade com o `sqlx` do desktop.

## Instalação no Debian

Copie esta pasta para o servidor e execute:

```sh
sudo sh install.sh
```

Abra **Sorta · Mídia** no Cockpit, cadastre `/mnt/hdd500` com a pasta `Midia`, salve a chave de API v3 do TMDB e faça a primeira varredura. O helper roda com o usuário da sessão do Cockpit, sem `sudo`; esse usuário precisa ler e escrever na pasta escolhida. A chave fica no arquivo de configuração do usuário com permissão `0600`, fora do HD de mídia. O pacote web fica em `/usr/share/cockpit/sorta`; o helper e as migrações ficam em `/usr/local/libexec/cockpit-sorta`.

No homelab atual, a pasta de catálogo é `/mnt/hdd500/Midia`. O cadastro guarda o UUID detectado no servidor; downloads em `/mnt/hdd500/downloads` ficam fora desta varredura.

O desktop e o Cockpit devem escrever no mesmo catálogo em momentos separados. SQLite serializa as transações do banco, mas a movimentação dos vídeos também precisa ser exclusiva. A primeira versão não coordena escritas simultâneas do desktop por Samba.

## Verificação

```sh
python3 -m unittest discover -s tests -v
node --check sorta.js
python3 -m py_compile helper.py
```

Os testes usam arquivos temporários pequenos e não acessam o TMDB nem o HD real. Antes de cada confirmação, a tela mostra todos os destinos. Se o disco for desconectado, ou seu UUID mudar, a operação para antes da gravação.
