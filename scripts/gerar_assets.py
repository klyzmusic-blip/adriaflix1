#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera o logo (512x512) e o fundo (1920x1080) do AdriaFlix no tema Ponyo 🌊:
ondas oceânicas, azul #4A90D9, rosa claro #FFB6C1 e estrelinha dourada #FFD700.

Os PNGs já vêm prontos em docs/assets/. Só rode este script se quiser
refazê-los (precisa de `pip install pillow`):

    python scripts/gerar_assets.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

AZUL = (74, 144, 217)
ROSA = (255, 182, 193)
OURO = (255, 215, 0)
MARINHO = (18, 49, 95)
BRANCO = (255, 255, 255)

FONTES_NEGRITO = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def gradiente(w, h, topo, base):
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        cor = tuple(int(topo[i] + (base[i] - topo[i]) * t) for i in range(3)) + (255,)
        d.line([(0, y), (w, y)], fill=cor)
    return img


def onda(w, h, y0, amplitude, periodo, fase, cor, passo=4):
    camada = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(camada)
    pontos = [(x, y0 + amplitude * math.sin(2 * math.pi * x / periodo + fase)) for x in range(0, w + passo, passo)]
    pontos += [(w, h), (0, h)]
    d.polygon(pontos, fill=cor)
    return camada


def brilho(img, cx, cy, raio, cor, desfoque):
    camada = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(camada).ellipse([cx - raio, cy - raio, cx + raio, cy + raio], fill=cor)
    return Image.alpha_composite(img, camada.filter(ImageFilter.GaussianBlur(desfoque)))


def estrela(img, cx, cy, raio, cor=OURO, com_brilho=True):
    if com_brilho:
        img = brilho(img, cx, cy, raio * 1.25, OURO + (120,), raio * 0.55)
    pts = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        r = raio if k % 2 == 0 else raio * 0.42
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    camada = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(camada).polygon(pts, fill=cor + (255,))
    return Image.alpha_composite(img, camada)


def bolha(img, cx, cy, r):
    camada = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(camada)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BRANCO + (28,), outline=BRANCO + (140,), width=max(2, int(r * 0.08)))
    rb = r * 0.28
    d.ellipse([cx - r * 0.5 - rb, cy - r * 0.5 - rb, cx - r * 0.5 + rb, cy - r * 0.5 + rb], fill=BRANCO + (170,))
    return Image.alpha_composite(img, camada)


def fonte_negrito(tamanho):
    for caminho in FONTES_NEGRITO:
        if Path(caminho).exists():
            return ImageFont.truetype(caminho, tamanho)
    return None


def gerar_logo(destino: Path, tamanho=512, s=2):
    w = tamanho * s
    img = gradiente(w, w, (126, 190, 244), (34, 88, 168))
    img = brilho(img, int(w * 0.27), int(w * 0.28), int(w * 0.17), ROSA + (255,), w * 0.018)

    # "A" de AdriaFlix
    fonte = fonte_negrito(int(w * 0.56))
    if fonte:
        camada = Image.new("RGBA", (w, w), (0, 0, 0, 0))
        d = ImageDraw.Draw(camada)
        d.text((w * 0.5, w * 0.40), "A", font=fonte, fill=BRANCO + (255,), anchor="mm")
        sombra = Image.new("RGBA", (w, w), (0, 0, 0, 0))
        ImageDraw.Draw(sombra).text((w * 0.5 + w * 0.012, w * 0.40 + w * 0.016), "A", font=fonte,
                                    fill=MARINHO + (110,), anchor="mm")
        img = Image.alpha_composite(img, sombra.filter(ImageFilter.GaussianBlur(w * 0.01)))
        img = Image.alpha_composite(img, camada)

    # ondas (de trás para frente)
    img = Image.alpha_composite(img, onda(w, w, w * 0.60, w * 0.030, w * 0.55, 0.3, BRANCO + (85,)))
    img = Image.alpha_composite(img, onda(w, w, w * 0.67, w * 0.034, w * 0.48, 1.7, ROSA + (190,)))
    img = Image.alpha_composite(img, onda(w, w, w * 0.75, w * 0.032, w * 0.60, 3.1, AZUL + (255,)))
    img = Image.alpha_composite(img, onda(w, w, w * 0.84, w * 0.028, w * 0.45, 0.9, (30, 78, 150, 255)))
    img = Image.alpha_composite(img, onda(w, w, w * 0.92, w * 0.022, w * 0.40, 2.4, MARINHO + (255,)))

    img = estrela(img, int(w * 0.79), int(w * 0.20), int(w * 0.095))
    for cx, cy, r in [(0.18, 0.78, 0.030), (0.30, 0.88, 0.020), (0.86, 0.80, 0.026), (0.72, 0.90, 0.016)]:
        img = bolha(img, int(w * cx), int(w * cy), int(w * r))

    # cantos arredondados
    mascara = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, w - 1, w - 1], radius=int(w * 0.22), fill=255)
    saida = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    saida.paste(img, (0, 0), mascara)
    saida = saida.resize((tamanho, tamanho), Image.LANCZOS)
    saida.save(destino, optimize=True)


def gerar_fundo(destino: Path, largura=1920, altura=1080, s=2):
    w, h = largura * s, altura * s
    rng = random.Random(2008)  # semente fixa: o fundo sai sempre igual

    img = gradiente(w, h, (10, 28, 66), (52, 112, 190))
    img = brilho(img, int(w * 0.5), int(h * 0.60), int(h * 0.55), ROSA + (95,), h * 0.16)

    # estrelinhas no céu
    for _ in range(46):
        x, y = rng.uniform(0.03, 0.97) * w, rng.uniform(0.03, 0.50) * h
        img = estrela(img, int(x), int(y), int(rng.uniform(0.004, 0.011) * w), com_brilho=False)
    img = estrela(img, int(w * 0.82), int(h * 0.17), int(w * 0.034))

    # ondas em camadas, do horizonte para a frente
    camadas = [
        (0.56, 0.012, 0.30, 0.2, BRANCO + (40,)),
        (0.60, 0.016, 0.26, 1.4, ROSA + (110,)),
        (0.66, 0.020, 0.34, 2.6, (96, 160, 228, 200)),
        (0.73, 0.024, 0.28, 0.7, ROSA + (80,)),
        (0.79, 0.026, 0.38, 3.4, AZUL + (235,)),
        (0.86, 0.028, 0.32, 1.9, (36, 92, 168, 250)),
        (0.93, 0.026, 0.42, 0.4, (22, 62, 124, 255)),
    ]
    for y0, amp, periodo, fase, cor in camadas:
        img = Image.alpha_composite(img, onda(w, h, h * y0, h * amp, w * periodo, fase, cor, passo=6))

    # bolhas
    for _ in range(26):
        x, y = rng.uniform(0.02, 0.98) * w, rng.uniform(0.62, 0.98) * h
        img = bolha(img, int(x), int(y), int(rng.uniform(0.008, 0.026) * h))

    # escurece levemente as bordas para o texto do Stremio ficar legível
    vinheta = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dv = ImageDraw.Draw(vinheta)
    dv.rectangle([0, 0, w, h], fill=MARINHO + (70,))
    img = Image.alpha_composite(img, vinheta)

    img = img.convert("RGB").resize((largura, altura), Image.LANCZOS)
    img.save(destino, optimize=True)


def gerar(pasta: Path) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    gerar_logo(pasta / "logo.png")
    gerar_fundo(pasta / "background.png")


if __name__ == "__main__":
    destino = Path(__file__).resolve().parent.parent / "docs" / "assets"
    gerar(destino)
    print(f"✅ Assets gerados em {destino}")
