"""
scraper_exemplo.py
Exemplo de boas práticas de web scraping:
 - verifica robots.txt
 - limites de taxa (rate limiting) com jitter
 - rotaciona User-Agent
 - respeita Retry-After e faz exponential backoff
 - timeout, logging, session reuse
 - opção de usar proxy (se legal/contratado)
Alvo de demonstração: http://books.toscrape.com/
"""

import time
import random
import logging
import requests
from urllib.parse import urljoin, urlparse
from urllib import robotparser
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- CONFIG ----------
BASE_URL = "http://books.toscrape.com/"
MAX_REQ_PER_MIN = 30  # limite global (ajuste conforme robots.txt)
MIN_DELAY = 1.0  # tempo mínimo entre requests (segundos)
MAX_DELAY = 3.0  # tempo máximo (jitter)
TIMEOUT = 10  # seconds para requests
PROXIES = None  # ex: {"http": "http://user:pass@proxy:port", ...}
USE_CACHE = True  # usar requests-cache em dev para reduzir tráfego
# ----------------------------

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)

# Lista simples de User-Agents; rotacione para variar cabeçalhos
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36",
]


def check_robots(url, user_agent="*"):
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as e:
        logging.warning(
            "Não foi possível ler robots.txt (%s): %s — prosseguindo com cautela",
            robots_url,
            e,
        )
        return None  # significa: não conseguimos verificar
    return rp.can_fetch(user_agent, url), rp


def make_session():
    session = requests.Session()
    # Retry strategy para erros transitórios
    retries = Retry(
        total=5,
        backoff_factor=1,  # base para exponential backoff
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Proxies (se configurado)
    if PROXIES:
        session.proxies.update(PROXIES)
    return session


def polite_get(session, url, robots_parser=None, user_agent=None):
    # Checar robots.txt (se disponível)
    if robots_parser is not None:
        allowed = robots_parser.can_fetch(user_agent or USER_AGENTS[0], url)
        if allowed is False:
            raise PermissionError(f"Bloqueado por robots.txt: {url}")
    # Cabeçalhos variáveis
    headers = {
        "User-Agent": user_agent or random.choice(USER_AGENTS),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # Timeout e tentativa com backoff são gerenciados pelo adapter+Retry
    resp = session.get(url, headers=headers, timeout=TIMEOUT)
    # Se servidor responder 429 e oferecer Retry-After, respeitar
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                wait = int(ra)
            except ValueError:
                # pode ser um HTTP-date; optar por backoff básico
                wait = 60
            logging.warning("Recebido 429. Respeitando Retry-After = %s segundos", wait)
            time.sleep(wait)
            resp = session.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def random_delay():
    # delay com jitter para parecer tráfego humano
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    logging.debug("Dormindo por %.2f s (jitter)", delay)
    time.sleep(delay)


def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    titles = []
    for item in soup.select("article.product_pod h3 a"):
        title = item.attrs.get("title")
        href = item.attrs.get("href")
        titles.append((title, href))
    return titles


def scrape_site(start_url):
    # configura sessão
    session = make_session()
    robots_ok, rp = check_robots(start_url, user_agent="*") or (None, None)
    if robots_ok is False:
        logging.error("Scrape cancelado: robots.txt proíbe acesso a %s", start_url)
        return
    logging.info("Iniciando scraping em: %s (robots.txt ok? %s)", start_url, robots_ok)

    page = start_url
    seen = set()
    while page:
        logging.info("Requisitando página: %s", page)
        try:
            html = polite_get(
                session, page, robots_parser=rp, user_agent=random.choice(USER_AGENTS)
            ).text
        except PermissionError as pe:
            logging.error(pe)
            break
        except requests.HTTPError as he:
            logging.error("HTTP error ao requisitar %s: %s", page, he)
            break
        except Exception as e:
            logging.exception("Erro inesperado ao requisitar %s: %s", page, e)
            break

        items = parse_listing(html)
        for title, href in items:
            # normaliza URL relativa
            item_url = urljoin(page, href)
            if item_url in seen:
                continue
            seen.add(item_url)
            logging.info("Encontrado livro: %s (%s)", title, item_url)
            # Aqui poderíamos fazer politeness antes de requisitar página do item
            random_delay()
            # (Opcional) repetir polite_get para a página do item e parsear detalhes

        # lógica simples para next page (site de exemplo)
        soup = BeautifulSoup(html, "html.parser")
        next_link = soup.select_one("li.next a")
        if next_link:
            page = urljoin(page, next_link.get("href"))
            # Adotar política de taxa entre páginas
            random_delay()
        else:
            page = None


if __name__ == "__main__":
    # Se desejar usar cache local durante desenvolvimento (reduz repetição de requests)
    if USE_CACHE:
        try:
            import requests_cache

            requests_cache.install_cache("scraper_cache", expire_after=3600)
            logging.info("requests-cache ativado (1h)")
        except Exception as e:
            logging.warning("requests-cache não disponível: %s", e)

    scrape_site(BASE_URL)
