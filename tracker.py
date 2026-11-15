#!/usr/bin/env python3
"""Rastreador de passagens baratas saindo de Brasília, com aviso no Telegram.

Uso:
  python tracker.py                 # roda uma rodada de consultas
  python tracker.py --descobrir-chat  # mostra seu chat id do Telegram
  python tracker.py --teste-telegram  # manda uma mensagem de teste

Variáveis de ambiente (nunca coloque no código):
  TELEGRAM_TOKEN    token dado pelo BotFather
  TELEGRAM_CHAT_ID  seu chat id
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
HISTORICO = BASE / "dados" / "historico.csv"
AVISADOS = BASE / "dados" / "avisados.json"
CAMPOS = ["data_consulta", "grupo", "sentido", "origem", "destino", "data_voo", "preco_brl", "escalas", "companhias"]


# ---------- consulta de preços ----------
def buscar_melhor_preco(origem, destino, data, max_escalas, moeda):
    """Voo só de ida mais barato em `data`. Devolve (preco, escalas, companhias) ou None."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    q = create_query(
        flights=[FlightQuery(date=data.isoformat(), from_airport=origem, to_airport=destino)],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        language="pt-BR",
        currency=moeda,
        max_stops=max_escalas,
    )
    resultados = get_flights(q)
    melhor = None
    for r in resultados:
        if not r.price:
            continue
        escalas = max(len(r.flights) - 1, 0)
        if melhor is None or r.price < melhor[0]:
            melhor = (r.price, escalas, ", ".join(r.airlines))
    return melhor


# ---------- histórico ----------
def ler_historico():
    if not HISTORICO.exists():
        return []
    with HISTORICO.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def gravar_historico(linhas):
    HISTORICO.parent.mkdir(parents=True, exist_ok=True)
    novo = not HISTORICO.exists()
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo:
            w.writeheader()
        w.writerows(linhas)


def ler_avisados():
    if AVISADOS.exists():
        return json.loads(AVISADOS.read_text(encoding="utf-8"))
    return {}


def gravar_avisados(d):
    AVISADOS.parent.mkdir(parents=True, exist_ok=True)
    AVISADOS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- regra de promoção ----------
def e_promocao(preco, grupo, trecho, historico, cfg, data_voo=None):
    """Promoção = abaixo do alvo do grupo OU bem abaixo da mediana histórica do destino."""
    motivos = []
    if grupo.get("fixa"):
        o, d = trecho.split("-")
        vistos = [float(h["preco_brl"]) for h in historico
                  if h["origem"] == o and h["destino"] == d and h["data_voo"] == data_voo]
        if not vistos or preco < min(vistos):
            motivos.append("menor preço já visto nesta data" if vistos else "primeira consulta desta data")
        return motivos
    alvo = grupo.get("alvo_brl")
    if alvo and preco <= alvo:
        motivos.append(f"abaixo do seu alvo de R$ {alvo:,.0f}".replace(",", "."))
    precos = [float(h["preco_brl"]) for h in historico if f'{h["origem"]}-{h["destino"]}' == trecho]
    if len(precos) >= cfg["minimo_historico_para_mediana"]:
        med = statistics.median(precos)
        if preco <= med * (1 - cfg["queda_vs_mediana"]):
            pct = round((1 - preco / med) * 100)
            motivos.append(f"{pct}% abaixo da mediana ({med:,.0f})".replace(",", "."))
    return motivos


def deve_avisar(chave, preco, avisados, cfg):
    anterior = avisados.get(chave)
    if anterior is None:
        return True
    return preco <= anterior * (1 - cfg["reavisar_se_cair_mais_que"])


# ---------- Telegram ----------
def telegram_enviar(texto):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[aviso não enviado: faltam TELEGRAM_TOKEN / TELEGRAM_CHAT_ID]\n" + texto)
        return False
    dados = urllib.parse.urlencode(
        {"chat_id": chat, "text": texto, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=dados)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


def telegram_descobrir_chat():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        sys.exit("Defina TELEGRAM_TOKEN primeiro.")
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
        dados = json.load(r)
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in dados.get("result", []) if "message" in u}
    if not chats:
        sys.exit("Nenhuma mensagem encontrada. Mande 'oi' para o seu bot e rode de novo.")
    for cid, nome in chats.items():
        print(f"chat id: {cid}  ({nome})")


def link_google_flights(origem, destino, data):
    q = f"Voos de {origem} para {destino} em {data.isoformat()} somente ida"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) + "&curr=BRL&hl=pt-BR"


def montar_mensagem(grupo, origem, destino, data, preco, escalas, cias, motivos):
    esc = "direto" if escalas == 0 else f"{escalas} escala(s)"
    sentido = "IDA" if origem == "BSB" else "VOLTA"
    return (
        f"✈️ {grupo['nome']} ({sentido}): {origem} → {destino}\n"
        f"R$ {preco:,.0f} só {sentido.lower()} ({esc})".replace(",", ".")
        + f"\nData: {data.strftime('%d/%m/%Y')}\n"
        f"Cia: {cias or '—'}\n"
        f"Por quê: {'; '.join(motivos)}\n"
        f"{link_google_flights(origem, destino, data)}"
    )


# ---------- rodada ----------
def planejar(cfg, hoje, rng):
    """Lista todos os trechos (ida e volta, separados) e sorteia um lote."""
    todas = []
    base = cfg["origem"]
    for g in cfg["grupos"]:
        d = g["primeira_data_em_dias"]
        while d <= g["horizonte_dias"]:
            data = hoje + timedelta(days=d)
            for dest in g["destinos"]:
                todas.append((g, base, dest, data))   # ida
                todas.append((g, dest, base, data))   # volta
            d += g["passo_dias"]
    rng.shuffle(todas)
    fixas = []
    for b in cfg.get("buscas_fixas", []):
        g = {"nome": b["nome"], "fixa": True, "max_escalas": b.get("max_escalas")}
        for t in b["trechos"]:
            for d in t["datas"]:
                fixas.append((g, t["origem"], t["destino"], date.fromisoformat(d)))
    return fixas + todas[: cfg["consultas_por_execucao"]]


def rodar(cfg, buscar=buscar_melhor_preco, enviar=telegram_enviar, hoje=None, dormir=time.sleep, rng=None):
    hoje = hoje or date.today()
    rng = rng or random.Random()
    historico = ler_historico()
    avisados = ler_avisados()
    novas, achados = [], []

    for g, orig, dest, data in planejar(cfg, hoje, rng):
        try:
            res = buscar(orig, dest, data, g.get("max_escalas"), cfg["moeda"])
        except Exception as e:  # um erro numa consulta não derruba a rodada
            print(f"falhou {orig}-{dest} {data}: {type(e).__name__}: {e}")
            dormir(rng.uniform(*cfg["pausa_segundos"]))
            continue
        if res:
            preco, escalas, cias = res
            novas.append({
                "data_consulta": hoje.isoformat(), "grupo": g["nome"],
                "sentido": "ida" if orig == cfg["origem"] else "volta",
                "origem": orig, "destino": dest, "data_voo": data.isoformat(),
                "preco_brl": preco, "escalas": escalas, "companhias": cias,
            })
            motivos = e_promocao(preco, g, f"{orig}-{dest}", historico, cfg, data.isoformat())
            chave = f"{orig}|{dest}|{data}"
            if motivos and deve_avisar(chave, preco, avisados, cfg):
                achados.append((chave, g, orig, dest, data, preco, escalas, cias, motivos))
        dormir(rng.uniform(*cfg["pausa_segundos"]))

    gravar_historico(novas)
    for chave, g, orig, dest, data, preco, escalas, cias, motivos in sorted(achados, key=lambda a: a[5]):
        if enviar(montar_mensagem(g, orig, dest, data, preco, escalas, cias, motivos)):
            avisados[chave] = preco
    gravar_avisados(avisados)
    print(f"{len(novas)} preços coletados, {len(achados)} promoções.")
    return novas, achados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--descobrir-chat", action="store_true")
    ap.add_argument("--teste-telegram", action="store_true")
    a = ap.parse_args()
    if a.descobrir_chat:
        return telegram_descobrir_chat()
    if a.teste_telegram:
        ok = telegram_enviar("✅ Bot de passagens do Gabriel conectado!")
        return print("enviado" if ok else "não enviado")
    rodar(json.loads(CONFIG.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
