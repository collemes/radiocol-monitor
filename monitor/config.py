"""Configurações do robô. Tudo pode ser alterado por variáveis de ambiente
(no GitHub: Settings > Secrets and variables > Actions > Variables)."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "sim", "yes", "on")


SITE_URL = os.environ.get("SITE_URL", "https://www.radiocol.com.br").rstrip("/")
# Identifica-se como navegador (alguns servidores de streaming recusam robôs)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
)

# Pastas (relativas à raiz do repositório)
DATA_DIR = os.environ.get("DATA_DIR", "data")
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", "docs")   # publicado pelo GitHub Pages
SHARDS = _int("SHARDS", 32)                         # nº de arquivos radios-XX.json

# Teste de streams
CONCURRENCY = _int("CONCURRENCY", 40)       # testes simultâneos
PER_HOST = _int("PER_HOST", 6)               # conexões simultâneas por servidor
CONNECT_TIMEOUT = _int("CONNECT_TIMEOUT", 10)
READ_TIMEOUT = _int("READ_TIMEOUT", 12)
MIN_AUDIO_BYTES = _int("MIN_AUDIO_BYTES", 4096)
# áudio com duração fechada até este limite (prévia do Spotify/Deezer/iTunes, vinheta): não é a rádio ao vivo
CLIPE_MAX_S = _int("CLIPE_MAX_S", 30)

# Quantas execuções diárias seguidas com falha antes de avisar por e-mail
# (evita alarme falso por queda momentânea da emissora)
CONFIRM_RUNS = _int("CONFIRM_RUNS", 2)
# rádios encerradas procuradas nos diretórios por execução (cada uma 1x por semana)
SUCESSORAS_MAX = _int("SUCESSORAS_MAX", 200)

# Busca de substitutos
MAX_SEARCH = _int("MAX_SEARCH", 600)          # máx. rádios pesquisadas por execução
AUTO_APPLY_SCORE = float(os.environ.get("AUTO_APPLY_SCORE", "0.88"))
SUGGEST_SCORE = float(os.environ.get("SUGGEST_SCORE", "0.6"))
USE_RADIO_BROWSER = _bool("USE_RADIO_BROWSER", True)
USE_OFFICIAL_SITE = _bool("USE_OFFICIAL_SITE", True)
COUNTRY_CODE = os.environ.get("COUNTRY_CODE", "BR")

# Páginas de player em popup que saíram do ar (ex.: o antigo blog radiosaovivo).
# O robô não tenta abrir esses sites; procura o stream no backup do Blogger
# (data/radiosaovivo.xml ou .xml.gz) e, se não houver, nas cópias do Internet Archive.
DEAD_POPUP_HOSTS = [h.strip().lower() for h in os.environ.get(
    "DEAD_POPUP_HOSTS", "radiosaovivo.radiocol.com.br").split(",") if h.strip()]
POPUP_BACKUP_FILES = ["radiosaovivo.xml", "radiosaovivo.xml.gz"]
USE_WAYBACK = _bool("USE_WAYBACK", True)
WAYBACK_API = os.environ.get("WAYBACK_API", "https://archive.org/wayback/available")
WAYBACK_WEB = os.environ.get("WAYBACK_WEB", "https://web.archive.org/web")

# Limite de posts (0 = todos). Útil para testar.
LIMIT_POSTS = _int("LIMIT_POSTS", 0)

# E-mail (defina como Secrets no GitHub)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("SMTP_PORT", 465)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER
EMAIL_ALWAYS = _bool("EMAIL_ALWAYS", False)   # True = manda e-mail todo dia
DRY_RUN_EMAIL = _bool("DRY_RUN_EMAIL", False)  # True = só grava o HTML em data/

# Link público do relatório completo (GitHub Pages), opcional
REPORT_URL = os.environ.get("REPORT_URL", "")

# "Tocou no Brasil": avisos do player dos visitantes, recebidos pelo Apps Script (mesmo web app do formulário).
# O robô roda nos EUA; esses avisos confirmam de dentro do Brasil se a rádio toca. Vazio = desligado.
TOCOU_URL = os.environ.get(
    "TOCOU_URL",
    "https://script.google.com/macros/s/AKfycbw_rfLs89RKAVD_TgY8UnC1efOAYNThCJsRMFP1s7p7zdrIl9nac6WB51nXOGdQYA9P/exec?acao=tocou")
TOCOU_DIAS_OK = _int("TOCOU_DIAS_OK", 3)      # tocou para alguém no Brasil nos últimos N dias = está no ar
TOCOU_ERROS = _int("TOCOU_ERROS", 3)          # N falhas no Brasil em 7 dias, sem nenhum "tocou" = fora do ar


# Navegador de verdade (Chromium/Playwright) para sites com player montado por JavaScript.
USE_BROWSER = _bool("USE_BROWSER", True)
BROWSER_PATH = os.environ.get("BROWSER_PATH", "")          # vazio = Chromium instalado pelo Playwright
MAX_BROWSER = _int("MAX_BROWSER", 250)                    # máx. sites abertos no navegador por execução
BROWSER_CONCURRENCY = _int("BROWSER_CONCURRENCY", 3)
BROWSER_SITE_S = _int("BROWSER_SITE_S", 50)              # tempo máximo por site
BROWSER_BUDGET_S = _int("BROWSER_BUDGET_S", 4800)         # tempo total do navegador por execução (80 min)
APLICAR_MIN_SCORE = float(os.environ.get("APLICAR_MIN_SCORE", "0.6"))   # sugestão >= isto já entra no site
WHATS_AUTO_MIN = float(os.environ.get("WHATS_AUTO_MIN", "0.95"))   # WhatsApp do site oficial acima disto entra no post

# tudoradio.com: índice por cidade/UF/frequência (data/tudoradio.json), montado aos poucos
USE_TUDORADIO = os.environ.get("USE_TUDORADIO", "1") != "0"
TUDORADIO_PAGINAS = _int("TUDORADIO_PAGINAS", 1200)      # páginas de rádio lidas por execução

# radios.com.br: listas por estado/cidade e páginas de rádio (data/radioscombr.json), aos poucos
USE_RADIOSCOMBR = os.environ.get("USE_RADIOSCOMBR", "1") != "0"
RADIOSCOMBR_LISTAS = _int("RADIOSCOMBR_LISTAS", 400)     # páginas de lista lidas por execução
RADIOSCOMBR_PAGINAS = _int("RADIOSCOMBR_PAGINAS", 1200)  # páginas de rádio lidas por execução

# antes de trocar um endereço existente, o robô abre o post no Radiocol e confere se toca (máximo por execução)
CONFERIR_RADIOCOL_MAX = _int("CONFERIR_RADIOCOL_MAX", 80)
# diretórios que recusam leitura simples: páginas abertas pelo navegador por execução
DIRETORIO_NAV_MAX = _int("DIRETORIO_NAV_MAX", 300)
DIRETORIO_DIAS = _int("DIRETORIO_DIAS", 30)               # cada cidade é relida nos diretórios a cada N dias
DIRETORIO_RCB_PAGINAS = _int("DIRETORIO_RCB_PAGINAS", 40)  # páginas da lista de uma cidade no radios.com.br


# muda esta data para zerar as filas de busca de novo (todos os posts sem áudio são procurados outra vez)
MUTIRAO = os.environ.get("MUTIRAO", "2026-11-23")
