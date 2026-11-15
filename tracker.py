#!/usr/bin/env python3
"""Rastreador de passagens baratas saindo de Brasília, com avisos no Telegram.

Uso:
  python tracker.py                   # rodada completa (promoções Livelo + comandos + preços)
  python tracker.py --promos          # só confere promoções Livelo -> LATAM Pass
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
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
HISTORICO = BASE / "dados" / "historico.csv"
AVISADOS = BASE / "dados" / "avisados.json"
ESTADO = BASE / "dados" / "estado.json"
CAMPOS = ["data_consulta", "grupo", "sentido", "origem", "destino", "data_voo", "preco_brl", "escalas", "companhias"]


# ---------- consulta de preços ----------
def buscar_melhor_preco(origem, destino, data, max_escalas, moeda, max_conexao_min=None, max_duracao_min=None):
    """Voo só de ida mais barato em `data`. Devolve (preco, escalas, companhias) ou None."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    q = create_query(
        flights=[FlightQuery(
            date=data.isoformat(), from_airport=origem, to_airport=destino,
            max_stops=max_escalas, max_layover_minutes=max_conexao_min, max_duration_minutes=max_duracao_min,
        )],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        language="pt-BR",
        currency=moeda,
    )
    melhor = None
    for r in get_flights(q):
        if not r.price:
            continue
        escalas = max(len(r.flights) - 1, 0)
        if melhor is None or r.price < melhor[0]:
            melhor = (r.price, escalas, ", ".join(r.airlines))
    return melhor


def buscar_com_tentativas(buscar, args, tentativas, dormir, rng, pausa):
    """O Google falha de forma intermitente: tenta de novo antes de desistir."""
    ultimo = None
    for i in range(tentativas):
        try:
            return buscar(*args)
        except Exception as e:  # noqa: BLE001
            ultimo = e
            if i < tentativas - 1:
                dormir(rng.uniform(*pausa) * (i + 1))
    raise ultimo


# ---------- arquivos de dados ----------
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


def _ler_json(caminho):
    if caminho.exists():
        return json.loads(caminho.read_text(encoding="utf-8"))
    return {}


def _gravar_json(caminho, d):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


ler_avisados = lambda: _ler_json(AVISADOS)  # noqa: E731
gravar_avisados = lambda d: _gravar_json(AVISADOS, d)  # noqa: E731
ler_estado = lambda: _ler_json(ESTADO)  # noqa: E731
gravar_estado = lambda d: _gravar_json(ESTADO, d)  # noqa: E731


# ---------- regra de promoção ----------
def fmt_brl(v):
    return f"R$ {v:,.0f}".replace(",", ".")


def e_promocao(preco, grupo, trecho, historico, cfg, data_voo=None):
    """Promoção = abaixo da meta do grupo OU bem abaixo da mediana histórica do trecho.
    Buscas fixas avisam sempre que aparece o menor preço já visto para a data."""
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
        motivos.append(f"abaixo da sua meta de {fmt_brl(alvo)}")
    precos = [float(h["preco_brl"]) for h in historico if f'{h["origem"]}-{h["destino"]}' == trecho]
    if len(precos) >= cfg["minimo_historico_para_mediana"]:
        med = statistics.median(precos)
        if preco <= med * (1 - cfg["queda_vs_mediana"]):
            pct = round((1 - preco / med) * 100)
            motivos.append(f"{pct}% abaixo da mediana ({fmt_brl(med)})")
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


def telegram_updates(offset):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&offset={offset}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r).get("result", [])


def telegram_descobrir_chat():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        sys.exit("Defina TELEGRAM_TOKEN primeiro.")
    chats = {}
    for u in telegram_updates(0):
        if "message" in u:
            c = u["message"]["chat"]
            chats[c["id"]] = c.get("first_name", "")
    if not chats:
        sys.exit("Nenhuma mensagem encontrada. Mande 'oi' para o seu bot e rode de novo.")
    for cid, nome in chats.items():
        print(f"chat id: {cid}  ({nome})")


# ---------- mensagens ----------
def link_google_flights(origem, destino, data):
    q = f"Voos de {origem} para {destino} em {data.isoformat()} somente ida"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) + "&curr=BRL&hl=pt-BR"


def linha_milhas(preco, cfg):
    mil = cfg.get("valor_milheiro_brl")
    if not mil:
        return ""
    milhas = round(preco / mil)  # em milhares de milhas
    return f"\nEm milhas compensa se custar até ~{milhas} mil milhas (milheiro a {fmt_brl(mil)})"


def montar_mensagem(grupo, origem, destino, data, preco, escalas, cias, motivos, cfg=None, base="BSB"):
    esc = "direto" if escalas == 0 else f"{escalas} escala(s)"
    sentido = "IDA" if origem == base else "VOLTA"
    return (
        f"✈️ {grupo['nome']} ({sentido}): {origem} → {destino}\n"
        f"{fmt_brl(preco)} só {sentido.lower()} ({esc})"
        f"\nData: {data.strftime('%d/%m/%Y')}\n"
        f"Cia: {cias or '—'}\n"
        f"Por quê: {'; '.join(motivos)}"
        + (linha_milhas(preco, cfg) if cfg else "")
        + f"\n{link_google_flights(origem, destino, data)}"
    )


def percentil(valores, p):
    v = sorted(valores)
    if not v:
        return None
    k = (len(v) - 1) * p
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def montar_resumo(historico, cfg, hoje, dias=7):
    corte = (hoje - timedelta(days=dias)).isoformat()
    rec = [h for h in historico if h["data_consulta"] >= corte]
    if not rec:
        return "📊 Resumo semanal: nenhum preço coletado nesta semana."
    linhas = [f"📊 Resumo semanal ({len(rec)} preços coletados)"]
    for g in cfg["grupos"]:
        do_grupo = [h for h in rec if h["grupo"] == g["nome"]]
        if not do_grupo:
            linhas.append(f"\n{g['nome']}: sem preços")
            continue
        linhas.append(f"\n{g['nome']}")
        for sentido in ("ida", "volta"):
            ss = [h for h in do_grupo if h["sentido"] == sentido]
            if ss:
                m = min(ss, key=lambda h: float(h["preco_brl"]))
                linhas.append(f"• {sentido}: {fmt_brl(float(m['preco_brl']))} {m['origem']}→{m['destino']} em {m['data_voo'][8:]}/{m['data_voo'][5:7]}/{m['data_voo'][:4]}")
        todos = [float(h["preco_brl"]) for h in historico if h["grupo"] == g["nome"]]
        if len(todos) >= 15 and g.get("alvo_brl"):
            sug = percentil(todos, 0.25)
            linhas.append(f"  meta atual {fmt_brl(g['alvo_brl'])} · histórico sugere ~{fmt_brl(sug)}")
    return "\n".join(linhas)


def responder_comando(texto, historico, cfg, hoje, estado):
    partes = texto.strip().split()
    cmd = partes[0].lower().split("@")[0] if partes else ""
    if cmd in ("/resumo", "/melhores"):
        return montar_resumo(historico, cfg, hoje)
    if cmd == "/status":
        u = estado.get("ultima_rodada", {})
        return (f"🤖 Última rodada: {u.get('data', '—')} · {u.get('coletados', 0)} preços, "
                f"{u.get('falhas', 0)} falhas · histórico com {len(historico)} preços.")
    if cmd == "/preco" and len(partes) >= 2:
        cods = [p.upper() for p in partes[1:3]]
        base = cfg["origem"]
        o, d = (cods[0], cods[1]) if len(cods) == 2 else (base, cods[0])
        rec = [h for h in historico if h["origem"] == o and h["destino"] == d]
        if not rec:
            return f"Ainda não tenho preços de {o}→{d} no histórico."
        mais = max(h["data_consulta"] for h in rec)
        melhor = min(rec, key=lambda h: float(h["preco_brl"]))
        ult = min((h for h in rec if h["data_consulta"] == mais), key=lambda h: float(h["preco_brl"]))
        return (f"{o}→{d}: menor preço já visto {fmt_brl(float(melhor['preco_brl']))} em {melhor['data_voo']}; "
                f"na última consulta ({mais}) o menor foi {fmt_brl(float(ult['preco_brl']))} em {ult['data_voo']}.")
    return "Comandos: /preco MIA · /preco BSB MIA · /resumo · /status"


def processar_comandos(historico, cfg, hoje, estado, enviar, buscar_updates=telegram_updates):
    chat_ok = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    offset = estado.get("telegram_offset", 0)
    try:
        updates = buscar_updates(offset)
    except Exception as e:  # noqa: BLE001
        print(f"comandos: não consegui ler o Telegram ({type(e).__name__})")
        return
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != chat_ok:
            continue  # só obedece o seu chat
        texto = m.get("text", "")
        if texto.startswith("/"):
            enviar(responder_comando(texto, historico, cfg, hoje, estado))
    estado["telegram_offset"] = offset


# ---------- promoções Livelo -> LATAM Pass ----------
def _titulos_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        yield (it.findtext("title") or "", it.findtext("link") or "", it.findtext("pubDate") or "")


def parece_promo_livelo_latam(titulo):
    t = titulo.lower()
    return "livelo" in t and "latam" in t and any(k in t for k in ("bônus", "bonus", "transfer", "milhas extras"))


def checar_promocoes(cfg, estado, enviar, agora=None, baixar=None):
    agora = agora or datetime.now(timezone.utc)
    avisadas = estado.setdefault("promos_avisadas", {})
    enviadas = 0
    for fonte in cfg.get("fontes_promocao", []):
        try:
            if baixar:
                xml = baixar(fonte)
            else:
                req = urllib.request.Request(fonte, headers={"User-Agent": "Mozilla/5.0 (bot-passagens)"})
                xml = urllib.request.urlopen(req, timeout=25).read()
            itens = list(_titulos_feed(xml))
        except Exception as e:  # noqa: BLE001
            print(f"promoções: falhou {fonte[:50]}: {type(e).__name__}")
            continue
        for titulo, link, pub in itens:
            if not parece_promo_livelo_latam(titulo):
                continue
            try:
                quando = parsedate_to_datetime(pub)
                if quando.tzinfo is None:
                    quando = quando.replace(tzinfo=timezone.utc)
                if agora - quando > timedelta(hours=cfg.get("promo_idade_max_horas", 48)):
                    continue
            except Exception:  # noqa: BLE001
                pass  # sem data confiável: deixa passar, o dedupe evita repetição
            pct = re.findall(r"(\d{2,3})\s*%", titulo)
            chave = f"{max(pct, key=int) if pct else titulo[:40]}|{agora.date().isoformat()}"
            if chave in avisadas:
                continue
            msg = f"🔔 Promoção Livelo → LATAM Pass\n{titulo.strip()}\n{link}"
            if enviar(msg):
                avisadas[chave] = agora.isoformat()
                enviadas += 1
    # limpa memória antiga (mantém 30 dias)
    corte = (agora - timedelta(days=30)).isoformat()
    for k in [k for k, v in avisadas.items() if v < corte]:
        del avisadas[k]
    print(f"promoções: {enviadas} aviso(s) enviado(s).")
    return enviadas


# ---------- rodada ----------
def planejar(cfg, hoje, rng):
    """Trechos fixos (sempre) + lote sorteado de ida e volta separadas."""
    todas = []
    base = cfg["origem"]
    for g in cfg["grupos"]:
        d = g["primeira_data_em_dias"]
        while d <= g["horizonte_dias"]:
            data = hoje + timedelta(days=d)
            for dest in g["destinos"]:
                todas.append((g, base, dest, data))
                todas.append((g, dest, base, data))
            d += g["passo_dias"]
    rng.shuffle(todas)
    fixas = []
    for b in cfg.get("buscas_fixas", []):
        g = {"nome": b["nome"], "fixa": True, "max_escalas": b.get("max_escalas")}
        for t in b["trechos"]:
            for d in t["datas"]:
                fixas.append((g, t["origem"], t["destino"], date.fromisoformat(d)))
    return fixas + todas[: cfg["consultas_por_execucao"]]


def rodar(cfg, buscar=buscar_melhor_preco, enviar=telegram_enviar, hoje=None, dormir=time.sleep, rng=None,
          baixar=None, buscar_updates=telegram_updates):
    hoje = hoje or date.today()
    rng = rng or random.Random()
    historico = ler_historico()
    avisados = ler_avisados()
    estado = ler_estado()
    novas, achados, falhas = [], [], 0

    # 1) promoções Livelo -> LATAM e comandos do Telegram (rápidos)
    checar_promocoes(cfg, estado, enviar, baixar=baixar)
    processar_comandos(historico, cfg, hoje, estado, enviar, buscar_updates)

    # 2) preços
    for g, orig, dest, data in planejar(cfg, hoje, rng):
        tentativas = cfg.get("tentativas_fixa", 5) if g.get("fixa") else cfg.get("tentativas", 3)
        args = (orig, dest, data, g.get("max_escalas"), cfg["moeda"],
                g.get("max_conexao_min", cfg.get("max_conexao_min")), g.get("max_duracao_min", cfg.get("max_duracao_min")))
        try:
            res = buscar_com_tentativas(buscar, args, tentativas, dormir, rng, cfg["pausa_segundos"])
        except Exception as e:  # noqa: BLE001
            falhas += 1
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
        if enviar(montar_mensagem(g, orig, dest, data, preco, escalas, cias, motivos, cfg, cfg["origem"])):
            avisados[chave] = preco
    gravar_avisados(avisados)
    historico_total = historico + novas

    # 3) alertas de saúde: bot quebrado?
    total = len(novas) + falhas
    if total and (not novas or falhas / total > cfg.get("limite_falhas_alerta", 0.85)):
        if estado.get("alerta_saude") != hoje.isoformat():
            if enviar(f"⚠️ Bot de passagens com problema: {falhas} de {total} consultas falharam "
                      f"({len(novas)} preços coletados). Veja o log no GitHub Actions."):
                estado["alerta_saude"] = hoje.isoformat()

    # 4) resumo semanal (domingo, uma vez)
    if hoje.weekday() == 6 and estado.get("ultimo_resumo") != hoje.isoformat():
        if enviar(montar_resumo(historico_total, cfg, hoje)):
            estado["ultimo_resumo"] = hoje.isoformat()

    estado["ultima_rodada"] = {"data": hoje.isoformat(), "coletados": len(novas), "falhas": falhas}
    gravar_estado(estado)
    print(f"{len(novas)} preços coletados, {falhas} falhas, {len(achados)} promoções.")
    return novas, achados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--promos", action="store_true")
    ap.add_argument("--descobrir-chat", action="store_true")
    ap.add_argument("--teste-telegram", action="store_true")
    a = ap.parse_args()
    if a.descobrir_chat:
        return telegram_descobrir_chat()
    if a.teste_telegram:
        ok = telegram_enviar("✅ Bot de passagens do Gabriel conectado!")
        return print("enviado" if ok else "não enviado")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if a.promos:
        estado = ler_estado()
        checar_promocoes(cfg, estado, telegram_enviar)
        return gravar_estado(estado)
    rodar(cfg)


if __name__ == "__main__":
    main()
