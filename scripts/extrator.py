#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AdriaFlix 🌊 - Extrator genérico de fontes por URL.

Você entrega uma URL e ele descobre sozinho o que é:

    • playlist M3U/M3U8 ............. (tratada pelo build_addon.py)
    • JSON de catálogo .............. (tratado pelo build_addon.py)
    • feed RSS / Atom / YouTube ..... itens com vídeo (enclosure, media:content, yt:videoId)
    • sitemap.xml ................... cada <loc> vira uma página de título
    • link direto de vídeo .......... .mp4 .m3u8 .webm .mpd ...
    • página HTML de vídeo .......... acha <video>, <source>, og:video, JSON-LD (VideoObject)
                                      e links de mídia escritos no código da página
    • página HTML de listagem ....... segue os cards/links e extrai cada página de título

O resultado sai no MESMO formato do provedor `custom_json` (nome, poster, streams,
episodios...), então tudo o que já existe no gerador continua valendo.

O que ele NÃO faz (de propósito): não executa JavaScript, não resolve cadeias de
redirecionamento/formulários, não quebra captcha nem proteção anti-robô. Se a mídia só
aparece depois de scripts ou de várias telas intermediárias, o título fica sem stream.

Boas maneiras: respeita o robots.txt (desligue com "respeitar_robots": false se a
fonte for sua), espera entre requisições (`pausa`) e se identifica como AdriaFlix.

Só usa a biblioteca padrão do Python (3.9+).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

USER_AGENT = "AdriaFlix/2.0 (addon Stremio estatico)"
MAX_BYTES = 6 * 1024 * 1024

# Menor número = melhor (mp4 toca em qualquer lugar; mkv/ogv/webm tocam mal no iPad).
EXT_RANK = {".mp4": 0, ".m4v": 0, ".m3u8": 1, ".webm": 2, ".mpd": 3, ".ogv": 4, ".mkv": 5}
_EXTS = "|".join(e[1:] for e in EXT_RANK)

RE_MIDIA_ABS = re.compile(
    r"https?://[^\s\"'<>\\)\]]+?\.(?:%s)(?:\?[^\s\"'<>\\)\]]*)?(?=[\"'\s<>\\)\]]|$)" % _EXTS, re.I
)
RE_MIDIA_REL = re.compile(
    r"[\"']((?:\.{0,2}/|//)?[^\"'\s<>\\]+?\.(?:%s)(?:\?[^\"'\s<>\\]*)?)[\"']" % _EXTS, re.I
)
RE_TRAILER = re.compile(r"(^|[/_\-. ])(sample|trailer|preview|teaser|promo)([_\-. ]|$)", re.I)
RE_EPISODIO = re.compile(
    r"(epis[oó]dio|episode|\bep\.?\s*\d+|\bs\d{1,2}\s*[._-]?\s*e\d{1,3}\b|(?<!\d)\d{1,2}x\d{1,3}(?!\d)|cap[ií]tulo)",
    re.I,
)
RE_UTIL = re.compile(
    r"/(login|entrar|cadastr\w*|register|signup|contato|contact|sobre|about|privacidade|privacy|termos|terms|"
    r"dmca|faq|ajuda|help|feed|rss|wp-json|wp-login|tag|author|autor|page|pagina)(/|$|\?)", re.I
)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}


class ExtracaoErro(Exception):
    pass


class PermissaoNegada(ExtracaoErro):
    pass


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, fonte: dict):
        self.limite = max(1, min(int(fonte.get("limite", 30)), 300))
        self.limite_episodios = max(1, min(int(fonte.get("limite_episodios", 40)), 300))
        self.paginas = max(1, min(int(fonte.get("paginas", 1)), 20))
        self.pausa = max(0.0, float(fonte.get("pausa", 0.6)))
        self.robots = fonte.get("respeitar_robots", True) is not False
        self.seguir_links = fonte.get("seguir_links", True) is not False
        self.permitir_externo = bool(fonte.get("permitir_externo", False))
        self.user_agent = str(fonte.get("user_agent") or USER_AGENT)
        self.incluir = re.compile(fonte["incluir"], re.I) if fonte.get("incluir") else None
        self.excluir = re.compile(fonte["excluir"], re.I) if fonte.get("excluir") else None
        sel = fonte.get("seletores") or {}
        self.cartao = sel.get("cartao") or {}
        self.pagina = sel.get("pagina") or {}


# --------------------------------------------------------------------------- #
# HTTP educado: robots.txt + ritmo entre requisições
# --------------------------------------------------------------------------- #
class Resposta:
    def __init__(self, url: str, ctype: str, texto: str, so_midia: bool = False):
        self.url, self.ctype, self.texto, self.so_midia = url, ctype, texto, so_midia


class Cliente:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ultimo: dict = {}
        self._robots: dict = {}
        self._trava = threading.Lock()

    def _esperar(self, host: str) -> None:
        with self._trava:
            agora = time.monotonic()
            quando = max(agora, self._ultimo.get(host, 0) + self.cfg.pausa)
            self._ultimo[host] = quando
        if quando > agora:
            time.sleep(quando - agora)

    def permitido(self, url: str) -> bool:
        if not self.cfg.robots:
            return True
        p = urllib.parse.urlparse(url)
        chave = f"{p.scheme}://{p.netloc}"
        with self._trava:
            rp = self._robots.get(chave)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(chave + "/robots.txt", headers={"User-Agent": self.cfg.user_agent})
                with urllib.request.urlopen(req, timeout=15) as r:
                    rp.parse(r.read(512 * 1024).decode("utf-8", "replace").splitlines())
            except Exception:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse([])  # robots.txt inacessível/ausente: nada proibido
            with self._trava:
                self._robots[chave] = rp
        agente = self.cfg.user_agent.split("/")[0].split()[0]
        return rp.can_fetch(agente, url)

    def buscar(self, url: str, referer: str | None = None) -> Resposta:
        url = _quote(url)
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            raise ExtracaoErro(f"URL não permitida (use http ou https): {url}")
        if not self.permitido(url):
            raise PermissaoNegada(f"o robots.txt de {p.netloc} não permite ler {p.path or '/'}")
        cab = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        }
        if referer:
            cab["Referer"] = referer
        espera, ultimo = 2, None
        for tentativa in range(3):
            self._esperar(p.netloc)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=30) as resp:
                    ctype = resp.headers.get("Content-Type", "") or ""
                    final = resp.geturl()
                    ct = ctype.lower()
                    if ct.startswith("video/") or (ct.startswith("audio/") and "mpegurl" not in ct):
                        return Resposta(final, ctype, "", so_midia=True)  # arquivo de mídia: não baixa o corpo
                    bruto = resp.read(MAX_BYTES)
                    cs = resp.headers.get_content_charset()
                return Resposta(final, ctype, _decodificar(bruto, cs))
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 404, 410, 451):
                    raise ExtracaoErro(f"HTTP {e.code} em {url}")
                ultimo = e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                ultimo = e
            if tentativa < 2:
                time.sleep(espera)
                espera *= 2
        raise ExtracaoErro(f"falha ao acessar {url}: {ultimo}")


def _quote(url: str) -> str:
    """Escapa acentos/espaços (já-escapado com % fica como está)."""
    return urllib.parse.quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~")


def unir(base: str, url: str) -> str:
    return _quote(urllib.parse.urljoin(base, url.strip()))


def _decodificar(bruto: bytes, charset: str | None) -> str:
    tentativas = [charset] if charset else []
    m = re.search(rb"charset=[\"']?([\w-]+)", bruto[:4096], re.I)
    if m:
        tentativas.append(m.group(1).decode("ascii", "ignore"))
    tentativas.append("utf-8")
    for c in tentativas:
        try:
            return bruto.decode(c, errors="strict").lstrip("\ufeff")
        except (LookupError, UnicodeDecodeError):
            continue
    return bruto.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Mini-DOM + seletores CSS (tag, .classe, #id, [attr], descendente e >)
# --------------------------------------------------------------------------- #
class No:
    __slots__ = ("tag", "attrs", "filhos", "pai")

    def __init__(self, tag: str, attrs: dict, pai):
        self.tag, self.attrs, self.filhos, self.pai = tag, attrs, [], pai


class _Construtor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = No("#raiz", {}, None)
        self.atual = self.raiz

    def handle_starttag(self, tag, attrs):
        no = No(tag, {k: (v or "") for k, v in attrs}, self.atual)
        self.atual.filhos.append(no)
        if tag not in VOID:
            self.atual = no

    def handle_startendtag(self, tag, attrs):
        self.atual.filhos.append(No(tag, {k: (v or "") for k, v in attrs}, self.atual))

    def handle_endtag(self, tag):
        n = self.atual
        while n is not None and n.tag != tag:
            n = n.pai
        if n is not None and n.pai is not None:
            self.atual = n.pai

    def handle_data(self, dados):
        self.atual.filhos.append(dados)


def parse_html(texto: str) -> No:
    c = _Construtor()
    c.feed(texto)
    c.close()
    return c.raiz


def percorrer(no: No, incluir_proprio: bool = True):
    pilha = [no]
    while pilha:
        n = pilha.pop()
        if incluir_proprio or n is not no:
            yield n
        for f in reversed(n.filhos):
            if isinstance(f, No):
                pilha.append(f)


def texto_de(no: No) -> str:
    partes, pilha = [], [no]
    while pilha:
        n = pilha.pop()
        if isinstance(n, str):
            partes.append(n)
            continue
        if n is not no and n.tag in ("script", "style", "noscript"):
            continue
        pilha.extend(reversed(n.filhos))
    return re.sub(r"\s+", " ", "".join(partes)).strip()


_RE_PECA = re.compile(
    r"#(?P<id>[\w-]+)|\.(?P<cl>[\w-]+)|"
    r"\[\s*(?P<an>[\w:-]+)\s*(?:(?P<op>[~^$*|]?=)\s*(?:\"(?P<v1>[^\"]*)\"|'(?P<v2>[^']*)'|(?P<v3>[^\]\s]*)))?\s*\]"
)


def _compound(tok: str) -> dict:
    comp = {"tag": None, "id": None, "classes": [], "attrs": []}
    m = re.match(r"\*|[A-Za-z][\w-]*", tok)
    pos = 0
    if m:
        comp["tag"] = None if m.group(0) == "*" else m.group(0).lower()
        pos = m.end()
    while pos < len(tok):
        m = _RE_PECA.match(tok, pos)
        if not m:
            raise ValueError(f"seletor CSS não suportado: '{tok}' (use tag, .classe, #id, [atributo] e espaço/>)")
        if m.group("id"):
            comp["id"] = m.group("id")
        elif m.group("cl"):
            comp["classes"].append(m.group("cl"))
        else:
            val = next((m.group(g) for g in ("v1", "v2", "v3") if m.group(g) is not None), None)
            comp["attrs"].append((m.group("an").lower(), m.group("op"), val))
        pos = m.end()
    return comp


def parse_seletor(sel: str) -> list:
    grupos = []
    for parte in sel.split(","):
        cadeia, comb = [], " "
        for t in re.findall(r">|[^\s>]+", parte.strip()):
            if t == ">":
                comb = ">"
                continue
            cadeia.append((comb, _compound(t)))
            comb = " "
        if cadeia:
            grupos.append(cadeia)
    return grupos


def _casa_comp(no: No, c: dict) -> bool:
    if c["tag"] and no.tag != c["tag"]:
        return False
    if c["id"] and no.attrs.get("id") != c["id"]:
        return False
    if c["classes"]:
        tem = set(no.attrs.get("class", "").split())
        if not all(k in tem for k in c["classes"]):
            return False
    for nome, op, val in c["attrs"]:
        if nome not in no.attrs:
            return False
        v = no.attrs[nome]
        if op is None:
            continue
        if op == "=" and v != val:
            return False
        if op == "^=" and not v.startswith(val):
            return False
        if op == "$=" and not v.endswith(val):
            return False
        if op == "*=" and val not in v:
            return False
        if op == "~=" and val not in v.split():
            return False
        if op == "|=" and not (v == val or v.startswith(val + "-")):
            return False
    return True


def _casa(no: No, cadeia: list, i: int) -> bool:
    comb, comp = cadeia[i]
    if not _casa_comp(no, comp):
        return False
    if i == 0:
        return True
    if comb == ">":
        return no.pai is not None and _casa(no.pai, cadeia, i - 1)
    p = no.pai
    while p is not None:
        if _casa(p, cadeia, i - 1):
            return True
        p = p.pai
    return False


def selecionar(escopo: No, sel: str) -> list:
    grupos = parse_seletor(sel)
    return [n for n in percorrer(escopo, False) if any(_casa(n, g, len(g) - 1) for g in grupos)]


def img_src(no: No) -> str | None:
    for k in ("data-src", "data-lazy-src", "data-original", "data-echo", "src"):
        v = (no.attrs.get(k) or "").strip()
        if v and not v.startswith("data:"):
            return v
    ss = (no.attrs.get("srcset") or no.attrs.get("data-srcset") or "").strip()
    if ss:
        return ss.split(",")[0].strip().split(" ")[0]
    return None


def valor(escopo: No, spec: str | None, padrao: str = "texto") -> str | None:
    """Lê 'seletor@atributo' (ou só 'seletor' → texto; só '@attr' → no próprio escopo)."""
    if not spec:
        return None
    sel, _, attr = spec.partition("@")
    sel, attr = sel.strip(), (attr.strip() or padrao)
    alvos = [escopo] if not sel else selecionar(escopo, sel)
    for n in alvos:
        if attr == "texto":
            v = texto_de(n)
        elif attr == "src" and n.tag == "img":
            v = img_src(n)
        else:
            v = (n.attrs.get(attr) or "").strip()
        if v:
            return v
    return None


# --------------------------------------------------------------------------- #
# Análise de uma página HTML
# --------------------------------------------------------------------------- #
def _flatten_ld(obj, saida: list) -> None:
    if isinstance(obj, list):
        for o in obj:
            _flatten_ld(o, saida)
    elif isinstance(obj, dict):
        saida.append(obj)
        if "@graph" in obj:
            _flatten_ld(obj["@graph"], saida)


def _tipos_ld(o: dict) -> set:
    t = o.get("@type")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _primeiro(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("@id") or v.get("name")
    return v if isinstance(v, str) else None


def analisar(raiz: No) -> dict:
    pg = {"metas": {}, "ld": [], "videos": [], "iframes": [], "ancoras": [], "h1": None, "titulo": None}
    for n in percorrer(raiz):
        t = n.tag
        if t == "meta":
            k = (n.attrs.get("property") or n.attrs.get("name") or n.attrs.get("itemprop") or "").lower()
            c = n.attrs.get("content", "").strip()
            if k and c:
                pg["metas"].setdefault(k, c)
        elif t == "script" and "ld+json" in n.attrs.get("type", "").lower():
            try:
                _flatten_ld(json.loads("".join(f for f in n.filhos if isinstance(f, str)).strip()), pg["ld"])
            except ValueError:
                pass
        elif t == "video" and n.attrs.get("src"):
            pg["videos"].append(n.attrs["src"])
        elif t == "source" and n.attrs.get("src") and (n.pai is not None and n.pai.tag in ("video", "audio")):
            pg["videos"].append(n.attrs["src"])
        elif t == "iframe" and (n.attrs.get("src") or n.attrs.get("data-src")):
            pg["iframes"].append(n.attrs.get("src") or n.attrs.get("data-src"))
        elif t == "a" and n.attrs.get("href"):
            pg["ancoras"].append(n)
        elif t == "h1" and not pg["h1"]:
            pg["h1"] = texto_de(n)
        elif t == "title" and not pg["titulo"]:
            pg["titulo"] = texto_de(n)
    return pg


def yt_id(url: str) -> str | None:
    m = re.search(
        r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:[^#]*&)?v=|shorts/)|youtu\.be/)([\w-]{11})", url or ""
    )
    return m.group(1) if m else None


def _ext(url: str) -> str:
    return os.path.splitext(urllib.parse.urlparse(url).path.lower())[1]


def _qualidade(url: str) -> str | None:
    m = re.search(r"(?<!\d)(2160|1440|1080|720|576|480|360|240)p?(?!\d)", urllib.parse.urlparse(url).path)
    return f"{m.group(1)}p" if m else None


def achar_midias(pg: dict, html_bruto: str, base: str) -> list:
    """Lista de URLs de mídia (melhores primeiro). Explícitas (<video>, og:video, JSON-LD) antes das achadas no código."""
    explicitas = list(pg["videos"])
    for k in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        v = pg["metas"].get(k)
        if v and (_ext(v) in EXT_RANK):
            explicitas.append(v)
    for o in pg["ld"]:
        v = o.get("contentUrl")
        if isinstance(v, str):
            explicitas.append(v)
    texto = html_bruto.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    achadas = [m.group(0) for m in RE_MIDIA_ABS.finditer(texto)]
    achadas += [m.group(1) for m in RE_MIDIA_REL.finditer(texto)]

    vistos, saida = set(), []
    for prio, grupo in ((0, explicitas), (1, achadas)):
        for u in grupo:
            u = unir(base, u.strip())
            if urllib.parse.urlparse(u).scheme not in ("http", "https") or u in vistos:
                continue
            if prio == 1 and _ext(u) not in EXT_RANK:
                continue
            vistos.add(u)
            saida.append((prio, EXT_RANK.get(_ext(u), 0), len(saida), u))
    if any(not RE_TRAILER.search(urllib.parse.urlparse(x[3]).path) for x in saida):
        saida = [x for x in saida if not RE_TRAILER.search(urllib.parse.urlparse(x[3]).path)]
    saida.sort()
    return [x[3] for x in saida]


def achar_youtube(pg: dict) -> list:
    ids = []
    for u in pg["iframes"] + [pg["metas"].get("og:video", ""), pg["metas"].get("og:video:url", "")]:
        i = yt_id(u)
        if i and i not in ids:
            ids.append(i)
    for o in pg["ld"]:
        i = yt_id(str(o.get("embedUrl") or ""))
        if i and i not in ids:
            ids.append(i)
    return ids


def _duracao_iso(s) -> str | None:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(s or ""))
    if not m or not any(m.groups()):
        return None
    minutos = int(m.group(1) or 0) * 60 + int(m.group(2) or 0) + round(float(m.group(3) or 0) / 60)
    return f"{minutos} min" if minutos > 0 else None


def _segundos_para_min(v) -> str | None:
    try:
        minutos = round(float(v) / 60)
    except (TypeError, ValueError):
        return None
    return f"{minutos} min" if minutos > 0 else None


def _limpar_titulo(t: str | None, site: str | None) -> str | None:
    if not t:
        return None
    t = re.sub(r"\s+", " ", t).strip()
    for sep in (" | ", " • ", " · "):
        if sep in t:
            t = t.split(sep)[0].strip()
    if site and t.lower().endswith(site.lower()) and len(t) > len(site) + 3:
        t = re.sub(r"\s*[-–—:]\s*$", "", t[: -len(site)]).strip()
    return t or None


def metadados(raiz: No, pg: dict, base: str, cfg: Config) -> dict:
    m = pg["metas"]
    ld = next((o for o in pg["ld"] if _tipos_ld(o) & {"movie", "videoobject", "tvseries", "tvepisode", "episode"}), {})
    sp = cfg.pagina
    site = m.get("og:site_name")

    titulo = (valor(raiz, sp.get("titulo")) or ld.get("name") or m.get("og:title")
              or m.get("twitter:title") or pg["h1"] or pg["titulo"])
    titulo = _limpar_titulo(titulo if isinstance(titulo, str) else None, site)

    descricao = (valor(raiz, sp.get("descricao")) or ld.get("description") or m.get("og:description")
                 or m.get("description") or m.get("twitter:description"))
    poster = (valor(raiz, sp.get("poster"), "src") or _primeiro(ld.get("image")) or _primeiro(ld.get("thumbnailUrl"))
              or m.get("og:image") or m.get("twitter:image"))
    ano_txt = (valor(raiz, sp.get("ano")) or ld.get("datePublished") or ld.get("dateCreated")
               or m.get("video:release_date") or m.get("article:published_time") or "")
    ano = re.search(r"\b(1[89]\d{2}|20\d{2})\b", str(ano_txt)) or re.search(r"\((1[89]\d{2}|20\d{2})\)", titulo or "")

    if sp.get("generos"):
        generos = [texto_de(n) for n in selecionar(raiz, sp["generos"].split("@")[0])]
    else:
        g = ld.get("genre") or m.get("video:tag") or m.get("keywords") or []
        generos = g if isinstance(g, list) else re.split(r"[;,]", str(g))

    return {
        "nome": titulo,
        "descricao": descricao if isinstance(descricao, str) else None,
        "poster": unir(base, poster) if isinstance(poster, str) and poster else None,
        "ano": int(ano.group(1)) if ano else None,
        "generos": [str(x).strip() for x in generos if str(x).strip()][:6],
        "duracao": _duracao_iso(ld.get("duration")) or _segundos_para_min(m.get("video:duration")),
    }


# --------------------------------------------------------------------------- #
# Montagem dos itens (formato custom_json)
# --------------------------------------------------------------------------- #
def _id_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    ultimo = [s for s in p.path.split("/") if s][-1:] or [p.netloc]
    nome = os.path.splitext(ultimo[0])[0] or ultimo[0]
    return re.sub(r"[^A-Za-z0-9_-]+", "_", nome).strip("_")[:40] + "_" + hashlib.md5(url.encode()).hexdigest()[:6]


def _streams(midias: list, yts: list, url_pagina: str, cfg: Config) -> list:
    streams = [{"url": u, "titulo": _qualidade(u)} for u in midias[:3]]
    if not streams:
        streams = [{"yt": i, "titulo": "YouTube"} for i in yts[:1]]
    if not streams and cfg.permitir_externo:
        streams = [{"externo": url_pagina, "titulo": "Abrir no site"}]
    return streams


def _nome_do_arquivo(url: str) -> str:
    base = os.path.splitext(os.path.basename(urllib.parse.urlparse(url).path))[0]
    return re.sub(r"[_.\-]+", " ", urllib.parse.unquote(base)).strip().title() or "Vídeo"


def detectar_temp_ep(texto: str):
    m = (re.search(r"s(\d{1,2})\s*[._-]?\s*e(\d{1,3})", texto, re.I)
         or re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", texto))
    if m:
        return int(m.group(1)), int(m.group(2))
    e = re.search(r"(?:epis[oó]dio|episode|\bep\.?)\s*(\d{1,3})", texto, re.I)
    if e:
        t = re.search(r"(?:temporada|season)\s*(\d{1,2})", texto, re.I)
        return (int(t.group(1)) if t else 1), int(e.group(1))
    return None


def _mesmo_site(a: str, b: str) -> bool:
    na = urllib.parse.urlparse(a).netloc.lower().removeprefix("www.")
    nb = urllib.parse.urlparse(b).netloc.lower().removeprefix("www.")
    return na == nb


def _filtro_url(cfg: Config, url: str) -> bool:
    if cfg.incluir and not cfg.incluir.search(url):
        return False
    if cfg.excluir and cfg.excluir.search(url):
        return False
    return True


def _links_episodios(raiz: No, pg: dict, base: str, cfg: Config) -> list:
    sel = cfg.pagina.get("episodios")
    achados, vistos = [], set()
    nos = selecionar(raiz, sel.split("@")[0]) if sel else pg["ancoras"]
    for a in nos:
        href = a.attrs.get("href", "")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        u = unir(base, href).split("#")[0]
        txt = texto_de(a) or a.attrs.get("title", "")
        if u == base or u in vistos or not _mesmo_site(u, base) or not _filtro_url(cfg, u):
            continue
        if not sel and not (RE_EPISODIO.search(txt) or RE_EPISODIO.search(urllib.parse.urlparse(u).path)):
            continue
        vistos.add(u)
        achados.append((u, txt))
    return achados


def _pagina_para_item(cli: Cliente, cfg: Config, tipo: str, url: str, cartao: dict | None = None,
                      resp: Resposta | None = None) -> dict | None:
    resp = resp or cli.buscar(url)
    if resp.so_midia:
        return _item_video_direto(url)
    raiz = parse_html(resp.texto)
    pg = analisar(raiz)
    base = resp.url
    meta = metadados(raiz, pg, base, cfg)
    cartao = cartao or {}
    nome = meta["nome"] or cartao.get("titulo") or _nome_do_arquivo(base)
    item = {
        "id": _id_url(url), "nome": nome, "ano": meta["ano"], "descricao": meta["descricao"],
        "poster": meta["poster"] or cartao.get("poster"), "generos": meta["generos"],
        "duracao": meta["duracao"], "link": url, "streams": [], "episodios": [],
    }
    sel_video = cfg.pagina.get("video")
    if sel_video:
        v = valor(raiz, sel_video, "src")
        midias = [unir(base, v)] if v else []
    else:
        midias = achar_midias(pg, resp.texto, base)
    yts = achar_youtube(pg)

    if tipo == "series":
        item["episodios"] = _episodios(cli, cfg, raiz, pg, base, midias, yts, nome)
        if not item["episodios"]:
            return None
        return item
    item["streams"] = _streams(midias, yts, url, cfg)
    return item if item["streams"] else None


def _episodios(cli, cfg, raiz, pg, base, midias, yts, nome_serie) -> list:
    links = _links_episodios(raiz, pg, base, cfg) if cfg.seguir_links else []
    brutos = []
    if links:
        def tarefa(par):
            u, txt = par
            try:
                r = cli.buscar(u, referer=base)
                if r.so_midia:
                    return txt, [u], []
                rz = parse_html(r.texto)
                p2 = analisar(rz)
                ms = [unir(r.url, valor(rz, cfg.pagina["video"], "src"))] \
                    if cfg.pagina.get("video") and valor(rz, cfg.pagina["video"], "src") \
                    else achar_midias(p2, r.texto, r.url)
                return txt or (p2["h1"] or ""), ms, achar_youtube(p2)
            except ExtracaoErro as e:
                print(f"   ⚠️  episódio {u}: {e}", flush=True)
                return txt, [], []

        with ThreadPoolExecutor(max_workers=3) as pool:
            for (u, _), (txt, ms, yy) in zip(links[: cfg.limite_episodios],
                                             pool.map(tarefa, links[: cfg.limite_episodios])):
                if ms or yy:
                    brutos.append((txt, _streams(ms, yy, u, cfg)))
    elif len(midias) > 1 and all(_qualidade(u) is None for u in midias):
        brutos = [(_nome_do_arquivo(u), [{"url": u}]) for u in midias[: cfg.limite_episodios]]
    elif midias or yts:
        brutos = [(nome_serie, _streams(midias, yts, base, cfg))]

    eps, usados = [], set()
    for pos, (titulo, streams) in enumerate(brutos, 1):
        achado = detectar_temp_ep(titulo or "")
        temporada, numero = achado if achado else (1, pos)
        while (temporada, numero) in usados:
            numero += 1
        usados.add((temporada, numero))
        eps.append({"temporada": temporada, "episodio": numero,
                    "titulo": (titulo or f"Episódio {numero}")[:120], "streams": streams})
    eps.sort(key=lambda e: (e["temporada"], e["episodio"]))
    return eps


def _item_video_direto(url: str) -> dict:
    return {"id": _id_url(url), "nome": _nome_do_arquivo(url), "link": url,
            "streams": [{"url": url, "titulo": _qualidade(url)}], "episodios": []}


# --------------------------------------------------------------------------- #
# Listagens (cards de uma página) e sitemap
# --------------------------------------------------------------------------- #
def _cartoes(raiz: No, pg: dict, base: str, cfg: Config) -> list:
    sc = cfg.cartao
    saida, vistos = [], set()

    def adicionar(href, titulo, poster):
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return
        u = unir(base, href).split("#")[0]
        if u in vistos or u.rstrip("/") == base.rstrip("/") or not _filtro_url(cfg, u):
            return
        vistos.add(u)
        saida.append({"url": u, "titulo": titulo, "poster": unir(base, poster) if poster else None})

    if sc.get("item"):
        for it in selecionar(raiz, sc["item"]):
            link = valor(it, sc.get("link") or "a@href", "href")
            adicionar(link, valor(it, sc.get("titulo")) or valor(it, "img@alt"),
                      valor(it, sc.get("poster") or "img", "src"))
        return saida

    com_imagem, so_texto = [], []
    for a in pg["ancoras"]:
        href = a.attrs.get("href", "")
        u = unir(base, href).split("#")[0] if href else ""
        if not u or not _mesmo_site(u, base) or RE_UTIL.search(urllib.parse.urlparse(u).path + "/"):
            continue
        p, dentro_menu = a.pai, False
        while p is not None:
            if p.tag in ("nav", "header", "footer", "aside"):
                dentro_menu = True
                break
            p = p.pai
        if dentro_menu:
            continue
        img = next((n for n in percorrer(a, False) if n.tag == "img"), None)
        titulo = texto_de(a) or a.attrs.get("title") or (img.attrs.get("alt") if img else "") or ""
        if img is not None:
            com_imagem.append((href, titulo.strip(), img_src(img)))
        elif len(titulo) >= 4 and len([s for s in urllib.parse.urlparse(u).path.split("/") if s]) >= 1:
            so_texto.append((href, titulo.strip(), None))
    for tupla in (com_imagem if len(com_imagem) >= 2 else com_imagem + so_texto):
        adicionar(*tupla)
    return saida


def _itens_de_listagem(cli, cfg, tipo, base, cartoes, limite) -> list:
    cartoes = cartoes[: limite * 2]  # folga: alguns links não terão vídeo

    def tarefa(c):
        try:
            return _pagina_para_item(cli, cfg, tipo, c["url"], cartao=c)
        except ExtracaoErro as e:
            print(f"   ⚠️  {c['url']}: {e}", flush=True)
            return None

    itens = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for r in pool.map(tarefa, cartoes):
            if r:
                itens.append(r)
            if len(itens) >= limite:
                break
    return itens


def _itens_sitemap(cli, cfg, tipo, raiz_xml, limite, profundidade=0) -> list:
    locs = [(e.text or "").strip() for e in raiz_xml.iter() if e.tag.rsplit("}", 1)[-1] == "loc" and e.text]
    if raiz_xml.tag.rsplit("}", 1)[-1] == "sitemapindex":
        itens = []
        filhos = [u for u in locs if _filtro_url(cfg, u)][:3] if profundidade < 1 else []
        for u in filhos:
            r = cli.buscar(u)
            itens += _itens_sitemap(cli, cfg, tipo, ET.fromstring(r.texto.encode("utf-8")), limite, profundidade + 1)
            if len(itens) >= limite:
                break
        return itens[:limite]
    cartoes = [{"url": u, "titulo": None, "poster": None} for u in locs if _filtro_url(cfg, u)]
    return _itens_de_listagem(cli, cfg, tipo, "", cartoes, limite)


# --------------------------------------------------------------------------- #
# RSS / Atom (inclui feeds do YouTube)
# --------------------------------------------------------------------------- #
def _l(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _itens_rss(cli, cfg, tipo, raiz_xml, url) -> list:
    feed_titulo = feed_img = None
    entradas = []
    for el in raiz_xml.iter():
        n = _l(el.tag)
        if n in ("item", "entry"):
            e = {"titulo": None, "link": None, "desc": None, "data": None, "midias": [], "yt": None, "thumb": None}
            for f in el.iter():
                k = _l(f.tag)
                if f is el:
                    continue
                if k == "title" and not e["titulo"] and (f.text or "").strip():
                    e["titulo"] = f.text.strip()
                elif k == "link":
                    href = f.attrib.get("href")
                    if href and f.attrib.get("rel", "alternate") == "alternate" and not e["link"]:
                        e["link"] = href
                    elif f.text and f.text.strip() and not e["link"]:
                        e["link"] = f.text.strip()
                    if href and f.attrib.get("rel") == "enclosure" and _ext(href) in EXT_RANK:
                        e["midias"].append(href)
                elif k in ("description", "summary", "content", "encoded") and not e["desc"] and f.text:
                    e["desc"] = f.text.strip()
                elif k in ("pubdate", "published", "updated", "date") and not e["data"] and f.text:
                    e["data"] = f.text.strip()
                elif k == "enclosure" and f.attrib.get("url"):
                    if f.attrib.get("type", "").startswith("video") or _ext(f.attrib["url"]) in EXT_RANK:
                        e["midias"].append(f.attrib["url"])
                elif k == "content" and f.attrib.get("url"):
                    if f.attrib.get("medium") == "video" or f.attrib.get("type", "").startswith("video") \
                            or _ext(f.attrib["url"]) in EXT_RANK:
                        e["midias"].append(f.attrib["url"])
                elif k == "thumbnail" and f.attrib.get("url") and not e["thumb"]:
                    e["thumb"] = f.attrib["url"]
                elif k == "image" and f.attrib.get("href") and not e["thumb"]:
                    e["thumb"] = f.attrib["href"]
                elif k == "videoid" and f.text:
                    e["yt"] = f.text.strip()
            entradas.append(e)
        elif n == "title" and feed_titulo is None and not entradas and (el.text or "").strip():
            feed_titulo = el.text.strip()
        elif n in ("image", "logo", "icon") and feed_img is None and not entradas:
            feed_img = (el.attrib.get("href") or el.attrib.get("url") or (el.text or "").strip()
                        or next((c.text for c in el if _l(c.tag) == "url" and c.text), None))
            feed_img = feed_img if feed_img and feed_img.startswith("http") else None
    base = url
    for e in entradas:
        e["link"] = unir(base, e["link"]) if e["link"] else None
        if not e["midias"] and not e["yt"] and e["link"]:
            e["yt"] = yt_id(e["link"])

    def streams_de(e):
        ms = sorted({u: EXT_RANK.get(_ext(u), 0) for u in e["midias"]}.items(), key=lambda kv: kv[1])
        s = [{"url": u, "titulo": _qualidade(u)} for u, _ in ms[:3]]
        if not s and e["yt"]:
            s = [{"yt": e["yt"], "titulo": "YouTube"}]
        if not s and e["link"] and cfg.seguir_links:
            try:
                r = cli.buscar(e["link"], referer=url)
                if not r.so_midia:
                    rz = parse_html(r.texto)
                    p2 = analisar(rz)
                    s = _streams(achar_midias(p2, r.texto, r.url), achar_youtube(p2), e["link"], cfg)
            except ExtracaoErro as ex:
                print(f"   ⚠️  {e['link']}: {ex}", flush=True)
        if not s and e["link"] and cfg.permitir_externo:
            s = [{"externo": e["link"], "titulo": "Abrir no site"}]
        return s

    ano = lambda e: (re.search(r"\b(1[89]\d{2}|20\d{2})\b", e["data"] or "") or [None, None])[1]
    itens = []
    if tipo == "series":
        entradas = list(reversed(entradas))[: cfg.limite_episodios]  # feeds listam o mais novo primeiro
        eps = []
        for n, e in enumerate(entradas, 1):
            s = streams_de(e)
            if s:
                eps.append({"temporada": 1, "episodio": n, "titulo": (e["titulo"] or f"Episódio {n}")[:120],
                            "streams": s, "thumbnail": e["thumb"]})
        if eps:
            itens.append({"id": _id_url(url), "nome": feed_titulo or "Série", "poster": feed_img or eps[0].get("thumbnail"),
                          "link": url, "streams": [], "episodios": eps})
        return itens
    for e in entradas[: cfg.limite * 2]:
        s = streams_de(e)
        if not s:
            continue
        itens.append({"id": _id_url(e["link"] or e["titulo"] or url) if (e["link"] or e["titulo"]) else _id_url(url),
                      "nome": e["titulo"] or "Vídeo", "ano": int(ano(e)) if ano(e) else None,
                      "descricao": re.sub(r"<[^>]+>", " ", e["desc"] or "") or None, "poster": e["thumb"],
                      "link": e["link"], "streams": s, "episodios": []})
        if len(itens) >= cfg.limite:
            break
    return itens


def _feed_youtube(url: str) -> str | None:
    """Converte página de playlist/canal do YouTube no feed Atom público correspondente."""
    p = urllib.parse.urlparse(url)
    if "youtube.com" not in p.netloc:
        return None
    q = urllib.parse.parse_qs(p.query)
    if p.path.startswith("/feeds/"):
        return None
    if p.path == "/playlist" and q.get("list"):
        return "https://www.youtube.com/feeds/videos.xml?playlist_id=" + q["list"][0]
    m = re.match(r"/channel/(UC[\w-]+)", p.path)
    if m:
        return "https://www.youtube.com/feeds/videos.xml?channel_id=" + m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Classificação e ponto de entrada
# --------------------------------------------------------------------------- #
def classificar(url: str, r: Resposta) -> str:
    if r.so_midia:
        return "video"
    t = r.texto.lstrip()[:2000]
    tl = t.lower()
    ct = r.ctype.lower()
    if t.startswith("#EXTM3U"):
        return "m3u" if "#EXTINF" in r.texto and "#EXT-X-" not in r.texto else "video"
    if "json" in ct or t[:1] in ("{", "["):
        try:
            json.loads(r.texto)
            return "json"
        except ValueError:
            pass
    if tl.startswith("<?xml") or "<rss" in tl[:600] or "<feed" in tl[:600] or "<urlset" in tl[:600] \
            or "<sitemapindex" in tl[:600]:
        return "xml"
    return "html"


def _expandir(url: str, paginas: int) -> list:
    if "{n}" not in url:
        return [url]
    return [url.replace("{n}", str(n)) for n in range(1, paginas + 1)]


def _uma_url(cli: Cliente, cfg: Config, tipo: str, url: str):
    feed = _feed_youtube(url)
    if feed:
        url = feed
    elif yt_id(url) and "list=" not in url:
        return "video", [{"id": _id_url(url), "nome": "Vídeo do YouTube", "link": url,
                          "streams": [{"yt": yt_id(url), "titulo": "YouTube"}], "episodios": []}]

    if _ext(url) in EXT_RANK:
        return "video", [_item_video_direto(url)]

    r = cli.buscar(url)
    tipo_conteudo = classificar(url, r)
    if tipo_conteudo == "video":
        return "video", [_item_video_direto(r.url)]
    if tipo_conteudo in ("m3u", "json"):
        return tipo_conteudo, None
    if tipo_conteudo == "xml":
        try:
            raiz_xml = ET.fromstring(r.texto.encode("utf-8"))
        except ET.ParseError as e:
            raise ExtracaoErro(f"XML inválido em {url}: {e}")
        if _l(raiz_xml.tag) in ("urlset", "sitemapindex"):
            return "sitemap", _itens_sitemap(cli, cfg, tipo, raiz_xml, cfg.limite)
        return "feed", _itens_rss(cli, cfg, tipo, raiz_xml, r.url)

    # HTML
    raiz = parse_html(r.texto)
    pg = analisar(raiz)
    base = r.url
    tem_cartao_manual = bool(cfg.cartao.get("item"))
    midias = [] if tem_cartao_manual else (
        [unir(base, valor(raiz, cfg.pagina["video"], "src"))]
        if cfg.pagina.get("video") and valor(raiz, cfg.pagina["video"], "src") else achar_midias(pg, r.texto, base))
    yts = [] if tem_cartao_manual else achar_youtube(pg)
    eh_pagina_de_serie = tipo == "series" and cfg.seguir_links and bool(_links_episodios(raiz, pg, base, cfg))

    if (midias or yts or eh_pagina_de_serie) and not tem_cartao_manual:
        item = _pagina_para_item(cli, cfg, tipo, url, resp=r)
        return "pagina", [item] if item else []

    if not cfg.seguir_links:
        item = _pagina_para_item(cli, cfg, tipo, url, resp=r)
        return "pagina", [item] if item else []

    cartoes = _cartoes(raiz, pg, base, cfg)
    if not cartoes:
        return "pagina", []
    return "listagem", _itens_de_listagem(cli, cfg, tipo, base, cartoes, cfg.limite)


def extrair(fonte: dict):
    """Devolve (formato, itens). Para 'm3u' e 'json' os itens vêm como None: o gerador trata esses formatos."""
    cfg = Config(fonte)
    cli = Cliente(cfg)
    tipo = fonte["tipo"]
    brutos, formato = [], None
    for u in _expandir(fonte["url"], cfg.paginas):
        try:
            fmt, itens = _uma_url(cli, cfg, tipo, u)
        except ExtracaoErro as e:
            if brutos or cfg.paginas > 1:
                print(f"   ⚠️  {u}: {e} (seguindo com o que já foi lido)", flush=True)
                break
            raise
        formato = formato or fmt
        if itens is None:
            return fmt, None
        brutos += itens
        if len(brutos) >= cfg.limite:
            break
    vistos, unicos = set(), []
    for b in brutos:
        if b["id"] not in vistos:
            vistos.add(b["id"])
            unicos.append(b)
    return formato, unicos[: cfg.limite]
