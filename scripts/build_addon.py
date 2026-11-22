#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AdriaFlix 🌊 - Gerador estático do addon de Stremio.

Lê o `fontes.json`, consulta as fontes cadastradas (qualquer URL via extrator automático,
Internet Archive, catálogos JSON próprios e playlists M3U) e escreve tudo como arquivos
estáticos em `docs/`,
prontos para o GitHub Pages:

    docs/manifest.json
    docs/index.html                       (página de instalação)
    docs/catalog/{type}/{id}.json         (fileiras)
    docs/catalog/{type}/{id}/genre=X.json (filtro por gênero)
    docs/meta/{type}/{id}.json
    docs/stream/{type}/{id}.json

Só usa a biblioteca padrão do Python (3.9+). Nenhum `pip install` necessário.

Uso:
    python scripts/build_addon.py
    python scripts/build_addon.py --base-url https://usuario.github.io/repo
    python scripts/build_addon.py --testar-url https://site.exemplo/filmes --tipo movie
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

ADDON_ID = "org.adriaflix.legal"
ADDON_NOME = "AdriaFlix 🌊"
ADDON_VERSAO = "2.0.0"
ADDON_DESCRICAO = (
    "Seu streaming favorito, do jeitinho da Ponyo 🌊 — "
    "filmes, séries e canais das fontes que você cadastra."
)
ID_PREFIXO = "adria_"
USER_AGENT = "AdriaFlix/2.0 (addon Stremio estatico)"

TIPOS_VALIDOS = {"movie", "series", "tv"}
IA_BUSCA = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/"
IA_DOWNLOAD = "https://archive.org/download/"
IA_IMAGEM = "https://archive.org/services/img/"
IA_DETALHES = "https://archive.org/details/"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extrator  # noqa: E402

LIMITE_MAXIMO = 300
MAX_EPISODIOS = 300

# Ordem de preferência dos formatos (menor = melhor). MP4 toca em qualquer lugar.
RANK_EXT = {".mp4": 0, ".m4v": 0, ".webm": 1, ".ogv": 2}
ROTULO_EXT = {".mp4": "MP4 Direto", ".m4v": "MP4 Direto", ".webm": "WebM Direto", ".ogv": "OGV Direto"}

GENEROS_IGNORADOS = {
    "feature films", "feature film", "movies", "movie", "film", "films", "video",
    "videos", "public domain", "open source", "opensource movies", "moving image",
    "full movie", "full length", "television", "tv", "classic tv", "archive",
}

EM_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def log(msg: str = "") -> None:
    print(msg, flush=True)


def aviso(msg: str) -> None:
    if EM_ACTIONS:
        print(f"::warning::{msg}", flush=True)
    else:
        print(f"⚠️  {msg}", flush=True)


def slug(texto, maximo: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(texto)).strip("._-")
    return (s or "item")[:maximo]


def como_lista(valor) -> list:
    if valor is None or valor == "":
        return []
    if isinstance(valor, (list, tuple)):
        return [str(v) for v in valor if v not in (None, "")]
    return [str(valor)]


def limpar_texto(valor, maximo: int = 1200) -> str:
    if isinstance(valor, (list, tuple)):
        valor = " ".join(str(v) for v in valor)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", str(valor or ""), flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = htmllib.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    if len(t) > maximo:
        t = t[:maximo].rsplit(" ", 1)[0].rstrip(".,;:— ") + "…"
    return t


def extrair_ano(*valores):
    for v in valores:
        m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", str(v or ""))
        if m:
            return int(m.group(1))
    return None


def formatar_duracao(segundos):
    try:
        s = float(segundos)
    except (TypeError, ValueError):
        return None
    return f"{round(s / 60)} min" if s > 0 else None


def formatar_tamanho(bytes_):
    try:
        b = float(bytes_)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return None
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    return f"{b / 1024 ** 2:.0f} MB"


def chave_natural(texto: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", texto)]


def url_http(valor) -> bool:
    try:
        return urllib.parse.urlparse(str(valor)).scheme in ("http", "https")
    except ValueError:
        return False


def normalizar_generos(valores, maximo: int = 4) -> list:
    saida = []
    for v in como_lista(valores):
        for parte in re.split(r"[;,]", v):
            g = re.sub(r"\s+", " ", parte).strip()
            if not g or len(g) > 24 or g.lower() in GENEROS_IGNORADOS:
                continue
            if not re.fullmatch(r"[A-Za-zÀ-ÿ0-9 \-]+", g):
                continue
            g = g.capitalize()
            if g not in saida:
                saida.append(g)
            if len(saida) >= maximo:
                return saida
    return saida


# --------------------------------------------------------------------------- #
# HTTP (apenas http/https, com tentativas)
# --------------------------------------------------------------------------- #
def http_get(url: str, tentativas: int = 4, timeout: int = 45) -> bytes:
    if not url_http(url):
        raise ValueError(f"URL não permitida (use http ou https): {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    )
    espera = 2
    ultimo_erro: Exception = RuntimeError("falha desconhecida")
    for tentativa in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410):
                raise
            ultimo_erro = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            ultimo_erro = e
        if tentativa < tentativas - 1:
            time.sleep(espera)
            espera *= 2
    raise ultimo_erro


def http_json(url: str):
    return json.loads(http_get(url).decode("utf-8-sig"))


def ler_fonte_texto(fonte: dict) -> str:
    """Lê o conteúdo de `url` (http/https) ou `arquivo` (dentro do repositório)."""
    if fonte.get("arquivo"):
        caminho = (RAIZ / fonte["arquivo"]).resolve()
        try:
            caminho.relative_to(RAIZ)
        except ValueError:
            raise ValueError("o campo 'arquivo' precisa apontar para dentro do repositório")
        return caminho.read_text(encoding="utf-8-sig")
    return http_get(fonte["url"]).decode("utf-8-sig", errors="replace")


# --------------------------------------------------------------------------- #
# Modelo interno de item
# --------------------------------------------------------------------------- #
def novo_item(fonte: dict, ident: str, nome: str, origem: str, origem_curta: str) -> dict:
    return {
        "id": f"{ID_PREFIXO}{fonte['id']}_{slug(ident)}",
        "type": fonte["tipo"],
        "name": nome,
        "year": None,
        "description": "",
        "poster": None,
        "poster_shape": "landscape",
        "background": None,
        "genres": [],
        "runtime": None,
        "origem": origem,              # texto completo exibido na sinopse
        "origem_curta": origem_curta,  # texto curto exibido na etiqueta do stream
        "licenca": None,
        "link": None,
        "streams": [],                 # filmes / canais
        "videos": [],                  # séries
    }


def rotulo_stream(origem_curta: str, formato: str, extra: str | None = None) -> str:
    partes = [f"📦 {origem_curta}", formato]
    if extra:
        partes.append(extra)
    return " • ".join(partes)


def formato_por_url(url: str) -> str:
    caminho = urllib.parse.urlparse(url).path.lower()
    if caminho.endswith(".m3u8"):
        return "HLS"
    if caminho.endswith(".mpd"):
        return "DASH"
    ext = os.path.splitext(caminho)[1]
    return ROTULO_EXT.get(ext, "Direto")


def descricao_final(item: dict) -> str:
    linhas = [f"📦 Origem: {item['origem']}"]
    if item.get("licenca"):
        linhas.append(f"⚖️ Licença: {item['licenca']}")
    texto = "\n".join(linhas)
    if item.get("description"):
        texto += "\n\n" + item["description"]
    return texto


# --------------------------------------------------------------------------- #
# Provedor: Internet Archive
# --------------------------------------------------------------------------- #
def ia_buscar(fonte: dict) -> list:
    limite = max(1, min(int(fonte.get("limite", 30)), LIMITE_MAXIMO))
    consulta = fonte["query"]
    if "access-restricted-item" not in consulta:
        # Itens de empréstimo/restritos não tocam sem login: ficam de fora.
        consulta = f"({consulta}) AND -access-restricted-item:true"
    params = [
        ("q", consulta),
        ("fl[]", "identifier"),
        ("rows", str(limite)),
        ("page", "1"),
        ("output", "json"),
        ("sort[]", fonte.get("ordenar", "downloads desc")),
    ]
    dados = http_json(IA_BUSCA + "?" + urllib.parse.urlencode(params))
    docs = (dados.get("response") or {}).get("docs") or []
    return [d["identifier"] for d in docs if d.get("identifier")]


def ia_origem(md: dict, fonte: dict):
    lic = md.get("licenseurl")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    baixo = (lic or "").lower()
    aberta = False
    if "publicdomain" in baixo or "/zero/" in baixo or "mark/1.0" in baixo:
        texto, aberta = "Internet Archive (Domínio Público)", True
    elif "creativecommons.org" in baixo:
        texto, aberta = "Internet Archive (Creative Commons)", True
    else:
        texto = "Internet Archive (coleção aberta — confira a licença)"
    # O usuário pode afirmar a origem de uma fonte inteira que ele mesmo curou.
    if fonte.get("rotulo_origem"):
        texto = str(fonte["rotulo_origem"])
    return texto, lic, aberta


def base_sem_derivados(nome: str) -> str:
    """Nome do arquivo sem extensão e sem sufixos de derivados (512kb, ia, h264...)."""
    base = os.path.splitext(os.path.basename(nome))[0]
    anterior = None
    while anterior != base:
        anterior = base
        base = re.sub(
            r"[_\-. ](512kb|256kb|1024kb|2048kb|ia|h264|mpeg4|mp4|ogv|webm|hd|sd|lo|low|high|web)$",
            "", base, flags=re.I,
        )
    return base


def ia_arquivos_video(arquivos: list) -> list:
    saida = []
    for f in arquivos:
        nome = f.get("name") or ""
        ext = os.path.splitext(nome)[1].lower()
        if ext not in RANK_EXT or str(f.get("private", "")).lower() == "true":
            continue
        if re.search(r"(^|[/_\-. ])(sample|trailer|preview)([_\-. ]|$)", nome, re.I):
            continue
        try:
            tamanho = int(f.get("size") or 0)
        except ValueError:
            tamanho = 0
        saida.append({
            "nome": nome,
            "ext": ext,
            "tamanho": tamanho,
            "duracao": f.get("length"),
            "titulo": limpar_texto(f.get("title") or "", 120),
            "base": base_sem_derivados(nome),
        })
    return saida


def agrupar_por_episodio(arquivos: list) -> dict:
    grupos: dict = {}
    for a in arquivos:
        grupos.setdefault(a["base"].lower(), []).append(a)
    return grupos


def escolher_streams(arquivos: list, ident: str, origem_curta: str) -> list:
    """Escolhe até 3 streams de um mesmo vídeo: o melhor MP4, um MP4 leve e uma alternativa."""
    ordenados = sorted(arquivos, key=lambda a: (RANK_EXT[a["ext"]], -a["tamanho"]))
    principal = ordenados[0]
    escolhidos = [(principal, None)]
    mp4s = [a for a in ordenados if RANK_EXT[a["ext"]] == 0]
    if len(mp4s) > 1 and principal in mp4s:
        leve = min(mp4s, key=lambda a: a["tamanho"] or 10 ** 15)
        if leve is not principal and leve["tamanho"] and leve["tamanho"] < 0.7 * principal["tamanho"]:
            escolhidos.append((leve, "leve"))
    if RANK_EXT[principal["ext"]] == 0:
        alternativas = [a for a in ordenados if RANK_EXT[a["ext"]] != 0]
        if alternativas:
            escolhidos.append((alternativas[0], None))

    streams = []
    for arq, marca in escolhidos[:3]:
        formato = ROTULO_EXT[arq["ext"]] + (" (leve)" if marca else "")
        url = IA_DOWNLOAD + urllib.parse.quote(ident) + "/" + urllib.parse.quote(arq["nome"], safe="/")
        streams.append({
            "url": url,
            "rotulo": rotulo_stream(origem_curta, formato, formatar_tamanho(arq["tamanho"])),
            "nao_web": RANK_EXT[arq["ext"]] != 0,
        })
    return streams


def detectar_temp_ep(texto: str):
    m = re.search(r"s(\d{1,2})[ ._-]?e(\d{1,3})", texto, re.I) or re.search(
        r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", texto
    )
    return (int(m.group(1)), int(m.group(2))) if m else None


def montar_episodios(item: dict, grupos: dict, montar_streams) -> None:
    """Transforma grupos de arquivos em temporadas/episódios do item (série)."""
    usados: set = set()
    videos = []
    chaves = sorted(grupos, key=chave_natural)[:MAX_EPISODIOS]
    iso = f"{item['year'] or 1970}-01-01T00:00:00.000Z"
    for posicao, chave in enumerate(chaves, 1):
        arqs = grupos[chave]
        achado = detectar_temp_ep(chave)
        temporada, episodio = achado if achado else (1, posicao)
        while (temporada, episodio) in usados:
            episodio += 1
        usados.add((temporada, episodio))
        titulo = next((a["titulo"] for a in arqs if a["titulo"]), "")
        if not titulo:
            bonito = re.sub(r"[_.]+", " ", arqs[0]["base"]).strip()
            titulo = bonito if bonito and not achado else f"Episódio {episodio}"
        videos.append({
            "id": f"{item['id']}_s{temporada}e{episodio}",
            "season": temporada,
            "episode": episodio,
            "title": titulo,
            "released": iso,
            "thumbnail": item["poster"],
            "streams": montar_streams(arqs),
        })
    videos.sort(key=lambda v: (v["season"], v["episode"]))
    item["videos"] = videos


def ia_processar(ident: str, fonte: dict):
    dados = http_json(IA_METADATA + urllib.parse.quote(ident))
    md = dados.get("metadata") or {}
    if not md or dados.get("is_dark"):
        return None
    if str(md.get("access-restricted-item", "")).lower() == "true":
        return None

    origem, licenca, aberta = ia_origem(md, fonte)
    if fonte.get("exigir_licenca_aberta") and not aberta:
        return None

    arquivos = ia_arquivos_video(dados.get("files") or [])
    if not arquivos:
        return None

    titulo = como_lista(md.get("title"))
    item = novo_item(fonte, ident, limpar_texto(titulo[0], 200) if titulo else ident, origem, "Internet Archive")
    item["year"] = extrair_ano(md.get("year"), md.get("date"))
    item["description"] = limpar_texto(md.get("description"))
    item["poster"] = IA_IMAGEM + urllib.parse.quote(ident)
    item["background"] = item["poster"]
    item["genres"] = normalizar_generos(md.get("subject"))
    item["licenca"] = licenca
    item["link"] = IA_DETALHES + urllib.parse.quote(ident)

    grupos = agrupar_por_episodio(arquivos)
    if fonte["tipo"] == "series":
        montar_episodios(item, grupos, lambda arqs: escolher_streams(arqs, ident, "Internet Archive"))
    else:
        # filme/canal: usa o vídeo maior do item (ignora extras e partes menores)
        chave = max(grupos, key=lambda k: max(a["tamanho"] for a in grupos[k]))
        escolhidos = grupos[chave]
        item["streams"] = escolher_streams(escolhidos, ident, "Internet Archive")
        principal = sorted(escolhidos, key=lambda a: (RANK_EXT[a["ext"]], -a["tamanho"]))[0]
        item["runtime"] = formatar_duracao(principal["duracao"])
    return item


def prov_internet_archive(fonte: dict) -> list:
    ids = ia_buscar(fonte)
    log(f"   {len(ids)} resultados na busca; lendo metadados dos itens…")

    def tarefa(ident):
        try:
            return ia_processar(ident, fonte)
        except Exception as e:  # um item ruim não derruba a fonte inteira
            log(f"   ⚠️  {ident}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        itens = list(pool.map(tarefa, ids))
    return [i for i in itens if i]


# --------------------------------------------------------------------------- #
# Provedor: custom_json (catálogo JSON próprio)
# --------------------------------------------------------------------------- #
ALIAS_POSTER = ("poster", "image", "imagem", "thumbnail", "thumb", "cover", "capa", "logo", "img")
ALIAS_URL = ("url", "video", "video_url", "stream", "stream_url", "src", "file", "arquivo", "link_video", "mp4", "hls")


def normalizar_bruto(b: dict) -> dict:
    """Aceita nomes alternativos de campos em JSONs de terceiros (image→poster, video→url...)."""
    b = dict(b)
    if not b.get("poster"):
        b["poster"] = next((b[k] for k in ALIAS_POSTER[1:] if isinstance(b.get(k), str) and b.get(k)), None)
    if not b.get("streams") and not b.get("url"):
        achado = next((b[k] for k in ALIAS_URL[1:] if isinstance(b.get(k), str) and url_http(b.get(k))), None)
        if achado:
            b["url"] = achado
    return b


def prov_custom_json(fonte: dict) -> list:
    bruto = json.loads(ler_fonte_texto(fonte))
    if isinstance(bruto, dict):
        lista = bruto.get("itens") or bruto.get("items") or bruto.get("catalogo") or bruto.get("movies") or []
    else:
        lista = bruto
    if not isinstance(lista, list):
        raise ValueError("o JSON precisa ser uma lista de itens (ou {\"itens\": [...]})")
    return itens_de_lista(fonte, lista)


def itens_de_lista(fonte: dict, lista: list, origem_padrao: str = "Catálogo próprio") -> list:
    """Converte itens no formato custom_json (do arquivo ou do extrator) em itens internos."""
    limite = min(int(fonte.get("limite", LIMITE_MAXIMO)), LIMITE_MAXIMO)
    itens = []
    for pos, b in enumerate(lista[:limite], 1):
        if not isinstance(b, dict):
            continue
        b = normalizar_bruto(b)
        nome = b.get("nome") or b.get("title") or b.get("name")
        if not nome:
            aviso(f"[{fonte['id']}] item #{pos} sem 'nome' — ignorado")
            continue
        curta = str(b.get("origem") or fonte.get("origem") or origem_padrao)
        item = novo_item(fonte, b.get("id") or nome, str(nome), f"{curta} (fonte cadastrada)", curta)
        item["year"] = extrair_ano(b.get("ano"), b.get("year"))
        item["description"] = limpar_texto(b.get("descricao") or b.get("description"))
        item["poster"] = b.get("poster") if url_http(b.get("poster")) else None
        item["background"] = b.get("fanart") if url_http(b.get("fanart")) else item["poster"]
        item["poster_shape"] = b.get("formato_poster") or fonte.get("formato_poster") or "poster"
        item["genres"] = normalizar_generos(b.get("generos") or b.get("genres"))
        item["runtime"] = b.get("duracao") or b.get("runtime")
        item["licenca"] = b.get("licenca") if url_http(b.get("licenca")) else (b.get("licenca") or None)
        item["link"] = b.get("link") if url_http(b.get("link")) else None

        def para_stream(s):
            if isinstance(s, dict) and s.get("yt"):  # vídeo do YouTube
                return {"yt": str(s["yt"]), "nao_web": False,
                        "rotulo": rotulo_stream(curta, "YouTube", s.get("titulo") if s.get("titulo") != "YouTube" else None)}
            if isinstance(s, dict) and s.get("externo") and url_http(s["externo"]):  # abre no navegador
                return {"externo": s["externo"], "nao_web": False, "rotulo": rotulo_stream(curta, "Abrir no site")}
            url = s.get("url") if isinstance(s, dict) else s
            if not url_http(url):
                return None
            titulo = s.get("titulo") if isinstance(s, dict) else None
            return {
                "url": url,
                "rotulo": rotulo_stream(curta, formato_por_url(url), titulo),
                "nao_web": formato_por_url(url) in ("OGV Direto", "WebM Direto", "DASH"),
            }

        if fonte["tipo"] == "series":
            episodios = b.get("episodios") or b.get("episodes") or []
            if not episodios:  # série com um único vídeo
                episodios = [{"temporada": 1, "episodio": 1, "titulo": nome, "streams": b.get("streams"), "url": b.get("url")}]
            usados: set = set()
            iso = f"{item['year'] or 1970}-01-01T00:00:00.000Z"
            for n, ep in enumerate(episodios, 1):
                if not isinstance(ep, dict):
                    continue
                brutos = ep.get("streams") or ([{"url": ep["url"]}] if ep.get("url") else [])
                streams = [s for s in (para_stream(x) for x in brutos) if s]
                if not streams:
                    continue
                temporada = int(ep.get("temporada") or ep.get("season") or 1)
                numero = int(ep.get("episodio") or ep.get("episode") or n)
                while (temporada, numero) in usados:
                    numero += 1
                usados.add((temporada, numero))
                item["videos"].append({
                    "id": f"{item['id']}_s{temporada}e{numero}",
                    "season": temporada,
                    "episode": numero,
                    "title": str(ep.get("titulo") or ep.get("title") or f"Episódio {numero}"),
                    "released": iso,
                    "thumbnail": ep.get("thumbnail") if url_http(ep.get("thumbnail")) else item["poster"],
                    "streams": streams,
                })
            item["videos"].sort(key=lambda v: (v["season"], v["episode"]))
            if not item["videos"]:
                aviso(f"[{fonte['id']}] '{nome}' sem episódios com URL válida — ignorado")
                continue
        else:
            brutos = b.get("streams") or ([{"url": b["url"]}] if b.get("url") else [])
            if not brutos and b.get("episodios"):
                brutos = (b["episodios"][0].get("streams") or [{"url": b["episodios"][0].get("url")}])
            item["streams"] = [s for s in (para_stream(x) for x in brutos) if s]
            if not item["streams"]:
                aviso(f"[{fonte['id']}] '{nome}' sem URL de vídeo http(s) — ignorado")
                continue
        itens.append(item)
    return itens


# --------------------------------------------------------------------------- #
# Provedor: m3u (playlists)
# --------------------------------------------------------------------------- #
def parse_m3u(texto: str) -> list:
    entradas, atual = [], None
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', linha))
            nome = linha.split(",", 1)[1].strip() if "," in linha else ""
            atual = {
                "nome": nome or attrs.get("tvg-name") or "",
                "logo": attrs.get("tvg-logo", ""),
                "grupo": attrs.get("group-title", ""),
            }
        elif linha and not linha.startswith("#") and atual is not None:
            atual["url"] = linha
            entradas.append(atual)
            atual = None
    return entradas


def prov_m3u(fonte: dict) -> list:
    entradas = parse_m3u(ler_fonte_texto(fonte))
    limite = min(int(fonte.get("limite", LIMITE_MAXIMO)), LIMITE_MAXIMO)
    curta = str(fonte.get("origem") or "Playlist M3U")
    itens = []
    for e in entradas:
        if not url_http(e["url"]) or not e["nome"]:
            continue
        sufixo = hashlib.md5(e["url"].encode("utf-8")).hexdigest()[:6]
        item = novo_item(fonte, f"{slug(e['nome'], 50)}_{sufixo}", e["nome"], f"{curta} (playlist cadastrada)", curta)
        item["poster"] = e["logo"] if url_http(e["logo"]) else None
        item["background"] = item["poster"]
        item["poster_shape"] = "square" if fonte["tipo"] == "tv" else "poster"
        item["genres"] = normalizar_generos(e["grupo"])
        stream = {
            "url": e["url"],
            "rotulo": rotulo_stream(curta, formato_por_url(e["url"])),
            "nao_web": False,
        }
        if fonte["tipo"] == "series":
            item["videos"] = [{
                "id": f"{item['id']}_s1e1", "season": 1, "episode": 1, "title": e["nome"],
                "released": "1970-01-01T00:00:00.000Z", "thumbnail": item["poster"], "streams": [stream],
            }]
        else:
            item["streams"] = [stream]
        itens.append(item)
        if len(itens) >= limite:
            break
    return itens


# --------------------------------------------------------------------------- #
# Provedor: auto (basta colar uma URL)
# --------------------------------------------------------------------------- #
def host_amigavel(url: str) -> str:
    h = urllib.parse.urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def prov_auto_ia(fonte: dict, url: str):
    """URLs do archive.org: página de um item, de uma coleção ou de uma busca."""
    p = urllib.parse.urlparse(url)
    f2 = dict(fonte)
    m = re.match(r"/details/([^/?#]+)", p.path)
    if m:
        ident = urllib.parse.unquote(m.group(1))
        md = (http_json(IA_METADATA + urllib.parse.quote(ident)).get("metadata") or {})
        if md.get("mediatype") == "collection":
            f2["query"] = f"collection:{ident} AND mediatype:movies"
            return prov_internet_archive(f2)
        item = ia_processar(ident, fonte)
        return [item] if item else []
    consulta = urllib.parse.parse_qs(p.query).get("query")
    if p.path.startswith("/search") and consulta:
        f2["query"] = consulta[0]
        return prov_internet_archive(f2)
    return None  # outra página do archive.org: segue pelo extrator genérico


def prov_auto(fonte: dict) -> list:
    url = fonte["url"]
    if host_amigavel(url).endswith("archive.org"):
        itens = prov_auto_ia(fonte, url)
        if itens is not None:
            return itens
    formato, brutos = extrator.extrair(fonte)
    log(f"   formato detectado: {formato}")
    if formato == "m3u":
        return prov_m3u(fonte)
    if formato == "json":
        return prov_custom_json(fonte)
    return itens_de_lista(fonte, brutos or [], origem_padrao=host_amigavel(url))


PROVEDORES = {
    "auto": prov_auto,
    "url": prov_auto,  # apelido
    "internet_archive": prov_internet_archive,
    "custom_json": prov_custom_json,
    "m3u": prov_m3u,
}


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
def normalizar_fontes(fontes: list) -> list:
    """Atalhos: uma fonte pode ser só a URL (texto) ou um objeto sem id/tipo/provedor/nome."""
    saida, usados = [], set()
    for f in fontes:
        if isinstance(f, str):
            f = {"url": f}
        if not isinstance(f, dict):
            saida.append(f)  # a validação abaixo reclama
            continue
        if f.get("ativo") is False:
            continue
        f = dict(f)
        if f.get("url") and not f.get("provedor"):
            f["provedor"] = "auto"
        f.setdefault("tipo", "movie")
        if f.get("url") and not f.get("nome"):
            f["nome"] = host_amigavel(f["url"]) or "Fonte"
        if f.get("url") and not f.get("id"):
            p = urllib.parse.urlparse(f["url"])
            bruto = host_amigavel(f["url"]) + "_" + p.path.strip("/")
            f["id"] = (re.sub(r"[^a-z0-9_-]+", "_", bruto.lower()).strip("_-")[:40] or "fonte")
            base, n = f["id"], 2
            while f["id"] in usados:
                f["id"] = f"{base}-{n}"
                n += 1
        usados.add(f.get("id"))
        saida.append(f)
    return saida


def carregar_fontes(caminho: Path) -> dict:
    try:
        cfg = json.loads(Path(caminho).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        sys.exit(f"❌ Arquivo não encontrado: {caminho}")
    except json.JSONDecodeError as e:
        sys.exit(
            f"❌ O fontes.json tem um erro de sintaxe (linha {e.lineno}, coluna {e.colno}): {e.msg}\n"
            "   Dica: confira vírgulas entre as fontes e se não sobrou vírgula depois da última."
        )
    fontes = cfg.get("fontes")
    if not isinstance(fontes, list) or not fontes:
        sys.exit('❌ O fontes.json precisa ter a lista "fontes" com pelo menos uma entrada.')
    cfg["fontes"] = fontes = normalizar_fontes(fontes)
    if not fontes:
        sys.exit('❌ Todas as fontes do fontes.json estão com "ativo": false.')

    erros, ids = [], set()
    for n, f in enumerate(fontes, 1):
        rot = f"fonte #{n} ({f.get('id', '?')})" if isinstance(f, dict) else f"fonte #{n}"
        if not isinstance(f, dict):
            erros.append(f"{rot}: precisa ser um objeto { '{...}' }")
            continue
        for campo in ("id", "nome", "tipo", "provedor"):
            if not f.get(campo):
                erros.append(f"{rot}: falta o campo '{campo}'")
        if f.get("id"):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(f["id"])):
                erros.append(f"{rot}: 'id' só pode ter letras, números, _ e -")
            if f["id"] in ids:
                erros.append(f"{rot}: 'id' repetido")
            ids.add(f["id"])
        if f.get("tipo") and f["tipo"] not in TIPOS_VALIDOS:
            erros.append(f"{rot}: 'tipo' deve ser um de {sorted(TIPOS_VALIDOS)}")
        prov = f.get("provedor")
        if prov and prov not in PROVEDORES:
            erros.append(f"{rot}: 'provedor' deve ser um de {sorted(PROVEDORES)}")
        if prov == "internet_archive" and not f.get("query"):
            erros.append(f"{rot}: falta o campo 'query' (busca do Internet Archive)")
        if prov in ("auto", "url") and not url_http(f.get("url")):
            erros.append(f"{rot}: informe 'url' começando com http:// ou https://")
        if prov in ("custom_json", "m3u") and not (f.get("url") or f.get("arquivo")):
            erros.append(f"{rot}: informe 'url' (http/https) ou 'arquivo' (caminho no repositório)")
    if erros:
        sys.exit("❌ Problemas no fontes.json:\n  - " + "\n  - ".join(erros))
    return cfg


def descobrir_base(cli: str | None, cfg: dict) -> str:
    base = cli or os.environ.get("ADRIAFLIX_BASE_URL") or cfg.get("base_url")
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY")
        if repo and "/" in repo:
            dono, nome = repo.split("/", 1)
            base = f"https://{dono.lower()}.github.io"
            if nome.lower() != f"{dono.lower()}.github.io":
                base += f"/{nome}"
    if not base:
        base = "https://USUARIO.github.io/REPOSITORIO"
        aviso("Não consegui descobrir a URL do GitHub Pages; usando um valor de exemplo. "
              "Use --base-url ou defina GITHUB_REPOSITORY.")
    return base.rstrip("/")


# --------------------------------------------------------------------------- #
# Saída (JSON do Stremio)
# --------------------------------------------------------------------------- #
def escrever_json(caminho: Path, obj) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def sem_vazios(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def entrada_catalogo(item: dict) -> dict:
    return sem_vazios({
        "id": item["id"],
        "type": item["type"],
        "name": item["name"],
        "poster": item["poster"],
        "posterShape": item["poster_shape"],
        "releaseInfo": str(item["year"]) if item["year"] else None,
        "genres": item["genres"],
    })


def meta_json(item: dict) -> dict:
    meta = sem_vazios({
        "id": item["id"],
        "type": item["type"],
        "name": item["name"],
        "poster": item["poster"],
        "posterShape": item["poster_shape"],
        "background": item["background"],
        "description": descricao_final(item),
        "releaseInfo": str(item["year"]) if item["year"] else None,
        "year": str(item["year"]) if item["year"] else None,
        "runtime": item["runtime"],
        "genres": item["genres"],
        "links": [{"name": "Fonte original", "category": "Origem", "url": item["link"]}] if item["link"] else [],
    })
    if item["type"] == "series":
        meta["videos"] = [
            sem_vazios({
                "id": v["id"], "title": v["title"], "season": v["season"], "episode": v["episode"],
                "released": v["released"], "thumbnail": v["thumbnail"],
            })
            for v in item["videos"]
        ]
    return meta


def stream_json(s: dict, grupo: str | None) -> dict:
    obj = {"name": "AdriaFlix 🌊", "title": s["rotulo"]}
    if s.get("yt"):
        obj["ytId"] = s["yt"]
    elif s.get("externo"):
        obj["externalUrl"] = s["externo"]
    else:
        obj["url"] = s["url"]
    dicas = {}
    if s.get("nao_web"):
        dicas["notWebReady"] = True
    if grupo:
        dicas["bingeGroup"] = grupo
    if dicas:
        obj["behaviorHints"] = dicas
    return obj


def generos_populares(itens: list, maximo: int = 12) -> list:
    contagem = Counter(g for i in itens for g in i["genres"])
    return [g for g, n in contagem.most_common() if n >= 2][:maximo]


def gerar_arquivos(resultados: list, destino: Path) -> list:
    """Escreve catalog/meta/stream em `destino`; devolve as entradas de catálogo do manifest."""
    catalogos = []
    for fonte, itens in resultados:
        tipo, fid = fonte["tipo"], fonte["id"]
        escrever_json(destino / "catalog" / tipo / f"{fid}.json", {"metas": [entrada_catalogo(i) for i in itens]})

        generos = generos_populares(itens)
        for g in generos:
            selecao = [entrada_catalogo(i) for i in itens if g in i["genres"]]
            escrever_json(destino / "catalog" / tipo / fid / f"genre={g}.json", {"metas": selecao})

        for i in itens:
            escrever_json(destino / "meta" / tipo / f"{i['id']}.json", {"meta": meta_json(i)})
            if tipo == "series":
                for v in i["videos"]:
                    streams = [stream_json(s, f"adriaflix-{i['id']}") for s in v["streams"]]
                    escrever_json(destino / "stream" / "series" / f"{v['id']}.json", {"streams": streams})
            else:
                streams = [stream_json(s, None) for s in i["streams"]]
                escrever_json(destino / "stream" / tipo / f"{i['id']}.json", {"streams": streams})

        entrada = {"type": tipo, "id": fid, "name": f"{fonte.get('icone', '')} {fonte['nome']}".strip()}
        if generos:
            entrada["extra"] = [{"name": "genre", "options": generos, "isRequired": False}]
        catalogos.append(entrada)
    return catalogos


def montar_manifest(base: str, catalogos: list) -> dict:
    return {
        "id": ADDON_ID,
        "version": ADDON_VERSAO,
        "name": ADDON_NOME,
        "description": ADDON_DESCRICAO,
        "logo": f"{base}/assets/logo.png",
        "background": f"{base}/assets/background.png",
        "resources": ["catalog", "meta", "stream"],
        "types": sorted({c["type"] for c in catalogos}),
        "idPrefixes": [ID_PREFIXO],
        "catalogs": catalogos,
        "behaviorHints": {"configurable": False, "configurationRequired": False, "adult": False, "p2p": False},
    }


PAGINA_INDEX = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AdriaFlix 🌊</title>
<link rel="icon" href="assets/logo.png">
<style>
  :root {{ --azul:#4A90D9; --marinho:#12315f; --rosa:#FFB6C1; --ouro:#FFD700; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(rgba(18,49,95,.78), rgba(18,49,95,.92)), url("assets/background.png") center/cover fixed;
  }}
  main {{ max-width: 640px; margin: 0 auto; padding: 40px 20px 60px; text-align: center; }}
  img.logo {{ width: 132px; height: 132px; border-radius: 28px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }}
  h1 {{ margin: 18px 0 6px; font-size: 2rem; }}
  p.desc {{ margin: 0 auto 26px; max-width: 460px; line-height: 1.5; opacity: .92; }}
  a.botao {{
    display: block; margin: 12px auto; max-width: 340px; padding: 15px 18px; border-radius: 14px;
    font-weight: 700; text-decoration: none; font-size: 1.05rem;
  }}
  a.principal {{ background: var(--rosa); color: var(--marinho); }}
  a.secundario {{ background: rgba(255,255,255,.14); color: #fff; border: 1px solid rgba(255,255,255,.3); }}
  .url {{
    margin: 14px auto 0; max-width: 520px; padding: 10px 12px; border-radius: 10px; word-break: break-all;
    background: rgba(0,0,0,.28); font: .85rem ui-monospace, Menlo, monospace; user-select: all;
  }}
  h2 {{ margin: 36px 0 12px; font-size: 1.1rem; color: var(--ouro); }}
  ul {{ list-style: none; padding: 0; margin: 0; text-align: left; }}
  li {{ padding: 10px 14px; margin: 8px 0; background: rgba(255,255,255,.1); border-radius: 12px; }}
  li small {{ opacity: .75; }}
  footer {{ margin-top: 36px; font-size: .8rem; opacity: .7; line-height: 1.5; }}
</style>
</head>
<body>
<main>
  <img class="logo" src="assets/logo.png" alt="Logo AdriaFlix">
  <h1>AdriaFlix 🌊</h1>
  <p class="desc">{descricao}</p>
  <a class="botao principal" href="{link_stremio}">⭐ Instalar no Stremio</a>
  <a class="botao secundario" href="{link_https}">📋 Abrir manifest.json</a>
  <div class="url">{link_https}</div>
  <h2>O que tem aqui</h2>
  <ul>
{lista}
  </ul>
  <footer>
    O AdriaFlix não hospeda vídeos: só organiza links das fontes cadastradas pelo dono do addon.<br>
    Atualizado em {data}.
  </footer>
</main>
</body>
</html>
"""


def gerar_index(base: str, resultados: list, destino: Path, descricao: str = ADDON_DESCRICAO) -> None:
    link_https = f"{base}/manifest.json"
    link_stremio = link_https.replace("https://", "stremio://", 1).replace("http://", "stremio://", 1)
    linhas = []
    for fonte, itens in resultados:
        singular, plural = {
            "movie": ("filme", "filmes"), "series": ("série", "séries"), "tv": ("canal", "canais"),
        }.get(fonte["tipo"], ("item", "itens"))
        rotulo = singular if len(itens) == 1 else plural
        linhas.append(
            f"    <li>{htmllib.escape(fonte.get('icone', ''))} <b>{htmllib.escape(fonte['nome'])}</b> "
            f"<small>— {len(itens)} {rotulo}</small></li>"
        )
    html = PAGINA_INDEX.format(
        descricao=htmllib.escape(descricao),
        link_stremio=htmllib.escape(link_stremio, quote=True),
        link_https=htmllib.escape(link_https),
        lista="\n".join(linhas),
        data=time.strftime("%d/%m/%Y", time.gmtime()),
    )
    (destino / "index.html").write_text(html, encoding="utf-8")


def garantir_assets(docs: Path) -> None:
    if (docs / "assets" / "logo.png").exists() and (docs / "assets" / "background.png").exists():
        return
    log("🎨 Gerando logo e fundo (assets ausentes)…")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gerar_assets  # noqa: WPS433

    gerar_assets.gerar(docs / "assets")


def testar_url(args) -> int:
    fonte = {"id": "teste", "nome": "Teste", "tipo": args.tipo, "provedor": "auto",
             "url": args.testar_url, "limite": max(1, args.limite)}
    if args.ignorar_robots:
        fonte["respeitar_robots"] = False
    log(f"🔎 Testando {args.testar_url}  (tipo: {args.tipo})")
    try:
        itens = prov_auto(fonte)
    except Exception as e:
        log(f"❌ Falhou: {e}")
        return 1
    if not itens:
        log("😕 Nada reproduzível foi encontrado. Dicas: a página pode montar o vídeo com JavaScript, "
            "exigir login, ou o robots.txt pode bloquear. Veja 'seletores' no README.")
        return 1
    for i in itens:
        log(f"\n• {i['name']}" + (f" ({i['year']})" if i["year"] else ""))
        if i["poster"]:
            log(f"    pôster: {i['poster']}")
        for s in i["streams"]:
            log(f"    ▶ {s.get('url') or s.get('yt') or s.get('externo')}   [{s['rotulo']}]")
        if i["videos"]:
            log(f"    {len(i['videos'])} episódio(s); primeiro: {i['videos'][0]['title']}")
    log(f"\n✅ {len(itens)} título(s). Se estiver bom, cole a URL no fontes.json.")
    return 0


# --------------------------------------------------------------------------- #
# Programa principal
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gera o addon estático AdriaFlix em docs/")
    ap.add_argument("--fontes", default=str(RAIZ / "fontes.json"), help="caminho do fontes.json")
    ap.add_argument("--saida", default=str(RAIZ / "docs"), help="pasta de saída (padrão: docs)")
    ap.add_argument("--base-url", help="URL pública do GitHub Pages (ex.: https://usuario.github.io/repo)")
    ap.add_argument("--testar-url", metavar="URL", help="só testa uma URL e mostra o que seria extraído (não gera nada)")
    ap.add_argument("--tipo", default="movie", choices=sorted(TIPOS_VALIDOS), help="tipo usado no --testar-url")
    ap.add_argument("--limite", type=int, default=5, help="quantos títulos mostrar no --testar-url")
    ap.add_argument("--ignorar-robots", action="store_true", help="no --testar-url: não consultar o robots.txt (só em site seu)")
    args = ap.parse_args(argv)

    if args.testar_url:
        return testar_url(args)

    cfg = carregar_fontes(Path(args.fontes))
    base = descobrir_base(args.base_url, cfg)
    saida = Path(args.saida)
    log(f"🌊 AdriaFlix — gerando addon para {base}")

    resultados, falhas = [], []
    for fonte in cfg["fontes"]:
        log(f"\n{fonte.get('icone', '•')} {fonte['nome']}  [{fonte['provedor']} → {fonte['tipo']}]")
        try:
            itens = PROVEDORES[fonte["provedor"]](fonte)
        except Exception as e:
            aviso(f"Fonte '{fonte['id']}' falhou: {e}")
            falhas.append(fonte["id"])
            continue
        if not itens:
            aviso(f"Fonte '{fonte['id']}' não retornou nenhum item reproduzível — ficou de fora.")
            falhas.append(fonte["id"])
            continue
        log(f"   ✅ {len(itens)} itens")
        resultados.append((fonte, itens))

    if not resultados:
        log("\n❌ Nenhuma fonte gerou itens. A pasta docs/ foi mantida como estava.")
        return 1

    # Gera tudo numa pasta temporária e só troca no final (nunca deixa docs/ pela metade).
    tmp = saida.parent / (saida.name + ".build_tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        catalogos = gerar_arquivos(resultados, tmp)
        saida.mkdir(parents=True, exist_ok=True)
        for sub in ("catalog", "meta", "stream"):
            alvo = saida / sub
            if alvo.exists():
                shutil.rmtree(alvo)
            if (tmp / sub).exists():
                shutil.move(str(tmp / sub), str(alvo))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    garantir_assets(saida)
    escrever_json(saida / "manifest.json", montar_manifest(base, catalogos))
    gerar_index(base, resultados, saida)
    (saida / ".nojekyll").write_text("", encoding="utf-8")

    total = sum(len(i) for _, i in resultados)
    log(f"\n🎉 Pronto! {len(resultados)} catálogo(s), {total} título(s).")
    log(f"   Manifest: {base}/manifest.json")
    if falhas:
        log(f"   ⚠️  Ficaram de fora: {', '.join(falhas)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
