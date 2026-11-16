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

# Quantas execuções diárias seguidas com falha antes de avisar por e-mail
# (evita alarme falso por queda momentânea da emissora)
CONFIRM_RUNS = _int("CONFIRM_RUNS", 2)

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
