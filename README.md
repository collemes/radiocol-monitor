# radiocol-monitor

Robô gratuito (GitHub Actions) que todo dia:

1. lê **todos os posts** do radiocol.com.br pelo feed do Blogger (inclusive os publicados ontem);
2. encontra o endereço de áudio de cada rádio no texto do post;
3. testa se o stream **realmente entrega áudio** (MP3/AAC, Shoutcast "ICY", Icecast, HLS `.m3u8`, `.pls`/`.m3u`) — uma página HTML ou um erro 404 contam como fora do ar;
4. para as rádios que caíram, **procura um endereço novo** sozinho:
   - variações do mesmo link (`http`→`https`, `/;`, `/stream`…);
   - outros links que já estão no post;
   - o **site oficial** da emissora (link no post) e o player embutido nele;
   - a base aberta **Radio Browser** (só aplica sozinho quando nome **e** frequência batem; senão vira sugestão);
5. grava o resultado em `docs/radios.json` (dividido em 32 arquivos pequenos em `docs/r/`), publicado pelo GitHub Pages;
6. manda **e-mail** só quando há novidade: rádios que o robô não conseguiu consertar, posts sem link de áudio e o que foi corrigido;
7. publica um relatório completo em `docs/relatorio.html`.

No Blogger, um script lê o `radios.json` e troca o link do player na hora — assim você **não precisa editar os 3.160 posts**.

**O link que você coloca à mão no post sempre prevalece.** Quando o robô percebe que você trocou o link de um post, ele passa a usar o seu, não o troca mais sozinho (se cair, só manda uma sugestão por e-mail) e o script do tema mantém o link do post mesmo antes da próxima rodada do robô.

```
Blogger (feed) ──► GitHub Actions (todo dia 05:17) ──► docs/r/NN.json ──► GitHub Pages ──► script no tema
                               │
                               └──► e-mail com o que precisa de revisão manual
```

Custo: **zero** (repositório público = minutos ilimitados no GitHub Actions; Pages é grátis).

---

## Instalação (uma vez, ~20 minutos)

### 1. Criar o repositório
1. No GitHub, crie um repositório **público** chamado `radiocol-monitor`.
2. Envie todos os arquivos desta pasta, **incluindo a pasta oculta `.github`**.
   - Mais fácil: GitHub Desktop (arrastar a pasta) ou `git push`.
   - Pelo site, a pasta `.github` às vezes não sobe no arrastar-e-soltar; nesse caso use
     *Add file → Create new file*, digite o nome `.github/workflows/monitor.yml` e cole o conteúdo.

### 2. Permitir que o robô grave no repositório
*Settings → Actions → General → Workflow permissions →* **Read and write permissions** → Save.

### 3. E-mail (Gmail)
1. Na conta Google que vai **enviar** os avisos, ative a verificação em duas etapas e crie uma
   **Senha de app** (Conta Google → Segurança → Senhas de app).
2. No repositório: *Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Valor |
|---|---|
| `SMTP_USER` | e-mail Gmail que envia |
| `SMTP_PASS` | a senha de app (16 letras) |
| `MAIL_TO` | quem recebe (ex.: redecolbrazil@gmail.com) |

As senhas ficam criptografadas no GitHub e não aparecem nos logs nem nos arquivos.

### 4. Publicar o radios.json (GitHub Pages)
*Settings → Pages → Build and deployment → Deploy from a branch →* branch `main`, pasta **`/docs`** → Save.
Em alguns minutos estará em `https://SEU-USUARIO.github.io/radiocol-monitor/`.

Opcional — na aba *Variables* crie `REPORT_URL` = `https://SEU-USUARIO.github.io/radiocol-monitor/relatorio.html`
para o e-mail trazer o link do relatório completo.

### 5. Feed do Blogger completo
Blogger → *Configurações → Feed do site → Permitir feed do blog* = **Completo**.
(Se estiver "Curto", o robô ainda funciona, mas abre post por post e fica mais lento.)

### 6. Primeiro teste
*Actions → Monitor de streams Radiocol → Run workflow* com **limit_posts = 50**.
Confira o resumo da execução, o arquivo `docs/relatorio.html` e o e-mail.
Depois rode com `0` (todos). A primeira execução completa leva de 15 a 40 minutos.

### 7. Script no tema do Blogger
Abra `blogger/radiocol-stream.html`, troque `SEU-USUARIO` e cole o bloco no
*Tema → Editar HTML*, logo antes de `</body>` (depois do script do MediaElement.js). O bloco
começa com `var RC_POST_ID = '<data:view.postId/>'`, que o Blogger preenche com o ID do post.

O script:
- descobre o ID do post, baixa só o arquivo `r/NN.json` correspondente (~10 KB, em cache);
- troca o link antigo pelo novo em qualquer atributo do post (`src`, `href`, `data-*`, `onclick`…);
- atualiza `<audio>` que não esteja tocando e elementos com `data-radiocol-stream`;
- dispara o evento `radiocol:stream` e deixa `window.RADIOCOL_STREAM` disponível para o player do tema;
- opcional: mostra um aviso discreto quando a rádio está confirmada fora do ar.

#### Posts antigos com iframe "Ao vivo"
O robô reconhece o `<iframe title="Ao vivo">` (maiúsculas e acentos não importam), descobre o stream
por trás dele — direto no `src`, num parâmetro como `?url=...` ou dentro da página do player — e procura
uma versão **https** (variação do link, site oficial, Radio Browser). Quando acha, o post ganha a marca
`"f":1` no JSON e o script do tema **troca o iframe pelo mesmo player MediaElement.js usado nos
posts** (`<center><div class="media-wrapper"><audio …><source … type="audio/mpeg">`), com
`type="application/x-mpegURL"` quando o stream é `.m3u8`. Se ainda só existir `http`, ou a rádio estiver fora do ar, o iframe fica
como está e o post aparece no relatório em "Iframes 'Ao vivo' ainda sem stream https".

Nos posts que já têm o `<audio>` do MediaElement, quando o robô troca o endereço o script atualiza o
`<source>` (inclusive o `type` para `.m3u8`) e chama `setSrc()` no player já criado — sem interromper
quem já está ouvindo.

> Se o player do tema guarda o link numa variável JavaScript ao carregar a página,
> faça-o ler `window.RADIOCOL_STREAM.u` (ou escutar o evento `radiocol:stream`) no clique de "play".

---

## Uso no dia a dia

**Recebeu o e-mail?** Para cada rádio listada:
- **Corrigir no post** (como sempre fez) → na próxima execução o robô detecta o link novo e passa a usá-lo; ou
- **Corrigir sem abrir o post** → edite `data/overrides.json` no próprio site do GitHub.
A chave pode ser a URL completa do post, só o caminho ou o ID do post:

```json
{
  "https://www.radiocol.com.br/2023/05/radio-exemplo-fm.html": {
    "stream": "https://novo.servidor.com/stream",
    "whatsapp": "84 98888-1111",
    "telefone": {"(84) 3421-0000": "(84) 3421-9999"}
  },
  "/2019/08/radio-que-fechou.html": {"ignore": true, "note": "emissora encerrou"}
}
```

- `stream`: novo endereço de áudio (o `type` vira `application/x-mpegURL` sozinho se for `.m3u8`).
- `whatsapp` / `telefone`: um número troca o WhatsApp/telefone que está no post; um mapa
  `{"antigo": "novo"}` serve para posts com vários números ou números sem link. O script troca
  os links `wa.me`, `api.whatsapp.com`, `tel:` **e** o número escrito no texto, em qualquer formato
  ("(84) 99988-7766", "84 9 9988 7766", "+55 84…"), sem mexer em números de outro DDD.

**Troca de frequência / emissora nova no lugar da antiga** (URL nova do post):
- A base usa o **ID do post**, não a URL. Se você mudar a URL do mesmo post, nada se perde, e o robô
  muda sozinho as chaves do `overrides.json` para a URL nova.
- Toda URL que deixou de existir (post com URL alterada ou post apagado) entra numa lista. O robô confere
  diariamente se ela responde **301**. Se responder 404, ou só um redirecionamento temporário (302), vem
  no e-mail com a sugestão de destino: o mesmo post na URL nova ou o post com título mais parecido.
  Você cria o redirecionamento em *Blogger → Configurações → Erros e redirecionamentos →
  Redirecionamentos personalizados* (marcando **Permanente**). A API do Blogger não permite criar
  redirecionamentos, por isso esse passo continua manual.

**WhatsApp que mudou**: uma vez por semana, para cada rádio com link do site oficial no post, o robô
lê o site e compara o WhatsApp de lá com o do post. Se for diferente, vem no e-mail como sugestão.
Ele **não troca sozinho**, porque um número errado é pior do que um desatualizado. Para aplicar, use
o `overrides.json`.

**Sugestões**: quando o robô acha um candidato mas não tem certeza (nome parecido, frequência
ausente), ele **não aplica** — mostra no e-mail como "Sugestão". Se estiver certo, copie para o `overrides.json`.

**Para rodar na hora**: *Actions → Run workflow*.

---

## Configurações (Variables do GitHub, todas opcionais)

| Variável | Padrão | Para quê |
|---|---|---|
| `CONFIRM_RUNS` | `2` | dias seguidos com falha antes de avisar (evita alarme por queda de minutos) |
| `EMAIL_ALWAYS` | `false` | `true` = e-mail todo dia, mesmo sem novidade |
| `SITE_URL` | `https://www.radiocol.com.br` | endereço do blog |
| `USE_JSDELIVR` | `false` | `true` se usar jsDelivr em vez do Pages (limpa o cache após cada atualização) |

Outras (`CONCURRENCY`, `MAX_SEARCH`, `AUTO_APPLY_SCORE`…) estão em `monitor/config.py`.

---

## Bom saber

- **http:// em site https**: navegadores bloqueiam ou tentam converter áudio `http://` dentro de páginas
  `https://` ("conteúdo misto"). O robô tenta a versão `https` e troca sozinho quando ela funciona; as que
  só existem em `http` aparecem no relatório como "Só funcionam em http".
- **Bloqueio por país**: os servidores do GitHub ficam nos EUA. Raras emissoras bloqueiam acesso de fora do
  Brasil e vão aparecer como fora do ar sem estar. Use o `"ignore": true` nesses casos.
- **Agendamento**: o GitHub pausa agendamentos de repositórios sem atividade por 60 dias; como o robô grava
  mudanças quase todo dia, isso não deve acontecer. Se acontecer, o GitHub avisa por e-mail e basta reativar.
- **O que é público**: `docs/` contém apenas os links de áudio (que já são públicos no site) e o relatório.
  Senhas ficam nos *Secrets*. O relatório tem `noindex` para não aparecer no Google.
- **Teste local**: `pip install aiohttp && python tests/run_local.py` simula um Blogger e 9 emissoras
  (MP3, Shoutcast ICY, HLS, página HTML, 404, site oficial com iframe, Radio Browser).

---

## Atualização pelas próprias emissoras (com sua aprovação)

```
Formulário do site ──► Apps Script ──► Planilha Google (privada) ──► você escolhe "Aprovar"
                                                                      │
                     site atualizado ◄── radios.json ◄── GitHub Actions ◄──┘   (+ e-mail ao solicitante)
```

- A emissora preenche o formulário que já existe no site.
- A solicitação vira uma linha numa **planilha Google só sua**, já com o post localizado e o stream que
  está hoje no site (e se ele está fora do ar), para você comparar com o que foi pedido. Você também recebe
  um e-mail de aviso, como hoje.
- Na coluna **Decisão** você escolhe **Aprovar**, **Aprovar remoção** ou **Recusar**. Antes de aprovar, pode corrigir as
  colunas marcadas como "editável" (ex.: um link digitado errado).
  - **Atualizar dados**: stream, WhatsApp e telefone vão para o site em poucos minutos, sem abrir o post.
    O robô testa o stream na hora e registra o resultado em *Actions*.
  - **Aprovar remoção**: o robô para de monitorar; apagar o post no Blogger continua sendo com você.
    (Um pedido "Remover / corrigir" aprovado com **Aprovar** é tratado como correção de dados — a remoção
    só acontece se você escolher explicitamente "Aprovar remoção".)
  - **Cadastrar nova rádio**: a linha fica marcada para você criar o post (por enquanto manual).
  - **Recusar**: o solicitante recebe um e-mail com o motivo (coluna "Motivo").
  - Para reenviar uma linha, apague a coluna "Situação" e escolha "Aprovar" de novo.
- Os demais campos (logo, gênero, cidade, frequência, "sobre"…) aparecem em "Outros dados" para você
  atualizar no post.
- **Segurança e privacidade**: nome, e-mail e telefone de quem pediu ficam só na planilha; para o GitHub
  vão apenas os dados públicos da emissora. O token do GitHub fica guardado no Apps Script, nunca no site.
  O robô recusa links inválidos e ignora qualquer campo extra. O campo "Não preencha" segura robôs
  de spam, e há limite de 5 envios por hora por e-mail. Nada vai ao ar sem a sua aprovação.

### Instalação (~15 minutos)

1. **Token do GitHub**: *GitHub → Settings → Developer settings → Fine-grained tokens → Generate new
   token* → *Only select repositories*: `radiocol-monitor` → *Repository permissions*: **Contents: Read and
   write** → gere e copie.
2. **Planilha**: crie uma planilha Google (com a conta do Radiocol) → *Extensões → Apps Script* → apague
   o conteúdo e cole `apps-script/Code.gs`. No topo, troque `SEU-USUARIO`.
3. *Configurações do projeto (engrenagem) → Propriedades do script → Adicionar*: `GITHUB_TOKEN` = o token.
4. No editor, escolha a função **configurar** e clique em *Executar* (autorize o acesso). Isso cria a aba
   "Solicitações", a lista Aprovar/Recusar e o gatilho.
5. *Implantar → Nova implantação → Tipo: App da Web* → *Executar como*: **Eu** · *Quem pode acessar*:
   **Qualquer pessoa** → copie a URL que termina em `/exec`.
6. Na página de cadastro do Blogger (modo HTML), substitua o conteúdo por `blogger/pagina-cadastro.html`
   e troque `COLE-AQUI-O-ID` pela URL do passo 5 (variável `RCF_FILA`). É a sua página, com o mesmo
   visual e as mesmas validações; muda só o envio:
   - vai primeiro para a fila de aprovação e mostra o **protocolo** ao solicitante;
   - se o Apps Script estiver fora do ar, envia pelo FormSubmit como hoje; se esse também falhar,
     oferece o envio pelo aplicativo de e-mail. Nenhuma solicitação se perde.
7. Teste: envie uma solicitação de atualização para uma rádio sua, aprove na planilha e acompanhe em
   *GitHub → Actions → Aplicar solicitação aprovada*.

Se você acrescentar campos ao formulário, inclua o `name` deles em `CAMPOS` ou `ROTULOS` no início do
`Code.gs`.
