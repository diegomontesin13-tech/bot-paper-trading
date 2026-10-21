"""Bot de paper trading sobre los mercados de Hyperliquid (los mismos que usa Liquid).

La cartera es 100% simulada: el bot nunca envía órdenes ni usa claves. Solo lee
datos públicos de https://api.hyperliquid.xyz/info, guarda el estado en
cartera.json, escribe diario.md y avisa por ntfy.
"""
import datetime as dt
import json
import math
import os
import time
import traceback
import urllib.request
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
RAIZ = Path(__file__).resolve().parent
CARTERA = RAIZ / "cartera.json"
DIARIO = RAIZ / "diario.md"

CAPITAL_INICIAL = 10_000.0
RIESGO = 0.01            # 1% del patrimonio por operación
MAX_POSICIONES = 5
MAX_NUEVAS = 2           # aperturas nuevas por ejecución
MAX_PREDICCIONES = 3
RIESGO_PREDICCION = 0.005
COMISION = 0.0005        # 0,05% por lado
DRAWDOWN_MAX = 0.15      # deja de abrir si se pierde un 15%
HORAS_MAX = 48           # cierre por tiempo
VENTAJA_MIN = 0.10       # 10 puntos de ventaja en predicciones
HUECO_MAX = 0.08         # diferencia máxima entre compra y venta en el libro de predicciones
VOLUMEN_MIN = 500_000    # volumen diario mínimo en USD
MAX_HISTORIAL = 3000

# categoría: (apalancamiento, stop mínimo en %, símbolos)
MERCADOS = {
    "cripto": (3, 0.006, ["BTC", "ETH", "SOL", "HYPE", "XRP", "BNB"]),
    "memecoin": (1, 0.03, ["DOGE", "kPEPE", "kSHIB", "kBONK", "WIF", "POPCAT", "PENGU", "FARTCOIN", "TRUMP"]),
    "accion": (3, 0.008, ["xyz:NVDA", "xyz:TSLA", "xyz:AAPL", "xyz:MSFT", "xyz:META", "xyz:GOOGL",
                          "xyz:AMZN", "xyz:AMD", "xyz:PLTR", "xyz:COIN", "xyz:MSTR"]),
    "indice": (3, 0.005, ["xyz:SP500", "xyz:XYZ100", "xyz:JP225", "xyz:KR200"]),
    "forex": (3, 0.002, ["xyz:EUR", "xyz:JPY", "xyz:DXY"]),
    "materia_prima": (3, 0.006, ["xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL", "xyz:NATGAS", "xyz:COPPER"]),
}
CATEGORIA = {s: cat for cat, (_, _, simbolos) in MERCADOS.items() for s in simbolos}


# ---------- datos ----------

def info(cuerpo):
    datos = json.dumps(cuerpo).encode()
    for intento in range(3):
        try:
            req = urllib.request.Request(API, data=datos, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if intento == 2:
                raise
            time.sleep(3 * (intento + 1))


def ms(fecha):
    return int(fecha.timestamp() * 1000)


def velas(simbolo, intervalo, desde_ms, hasta_ms):
    time.sleep(0.5)  # no saturar el límite de la API
    filas = info({"type": "candleSnapshot",
                  "req": {"coin": simbolo, "interval": intervalo, "startTime": desde_ms, "endTime": hasta_ms}})
    return [{"t": f["t"], "o": float(f["o"]), "h": float(f["h"]), "l": float(f["l"]),
             "c": float(f["c"]), "v": float(f["v"])} for f in filas]


def contextos():
    """Precio y volumen diario de cada perpetuo (dex principal y xyz)."""
    precios, volumen = {}, {}
    for dex in ("", "xyz"):
        cuerpo = {"type": "metaAndAssetCtxs"}
        if dex:
            cuerpo["dex"] = dex
        meta, ctxs = info(cuerpo)
        for activo, ctx in zip(meta["universe"], ctxs):
            px = ctx.get("midPx") or ctx.get("markPx")
            if px:
                precios[activo["name"]] = float(px)
                volumen[activo["name"]] = float(ctx.get("dayNtlVlm") or 0)
    return precios, volumen


# ---------- indicadores ----------

def ema(valores, n):
    k, e, salida = 2 / (n + 1), valores[0], []
    for v in valores:
        e = v * k + e * (1 - k)
        salida.append(e)
    return salida


def rsi(cierres, n=14):
    subidas = bajadas = 0.0
    for i in range(1, n + 1):
        d = cierres[i] - cierres[i - 1]
        subidas += max(d, 0)
        bajadas += max(-d, 0)
    subidas, bajadas = subidas / n, bajadas / n
    for i in range(n + 1, len(cierres)):
        d = cierres[i] - cierres[i - 1]
        subidas = (subidas * (n - 1) + max(d, 0)) / n
        bajadas = (bajadas * (n - 1) + max(-d, 0)) / n
    return 100.0 if bajadas == 0 else 100 - 100 / (1 + subidas / bajadas)


def atr(vs, n=14):
    rangos = [max(v["h"] - v["l"], abs(v["h"] - p["c"]), abs(v["l"] - p["c"])) for p, v in zip(vs, vs[1:])]
    a = sum(rangos[:n]) / n
    for r in rangos[n:]:
        a = (a * (n - 1) + r) / n
    return a


def volatilidad_hora(vs):
    rend = [math.log(b["c"] / a["c"]) for a, b in zip(vs, vs[1:]) if a["c"] > 0 and b["c"] > 0]
    if len(rend) < 20:
        return None
    media = sum(rend) / len(rend)
    return math.sqrt(sum((r - media) ** 2 for r in rend) / (len(rend) - 1))


def senal(vs):
    """Tendencia + momento sobre velas de 1h. Devuelve (lado, fuerza, atr) o None."""
    cierres = [v["c"] for v in vs]
    e20, e50, e100 = ema(cierres, 20), ema(cierres, 50), ema(cierres, 100)
    r, a, c = rsi(cierres), atr(vs), cierres[-1]
    if a <= 0:
        return None
    pendiente = e20[-1] - e20[-4]
    fuerza = abs(e20[-1] - e50[-1]) / a
    if c > e20[-1] > e50[-1] > e100[-1] and pendiente > 0 and 50 <= r <= 70:
        return "long", fuerza, a
    if c < e20[-1] < e50[-1] < e100[-1] and pendiente < 0 and 30 <= r <= 50:
        return "short", fuerza, a
    return None


def tendencia_rota(vs, lado):
    cierres = [v["c"] for v in vs]
    e50 = ema(cierres, 50)[-1]
    return cierres[-1] < e50 if lado == "long" else cierres[-1] > e50


# ---------- cartera ----------

def cargar():
    if CARTERA.exists():
        return json.loads(CARTERA.read_text(encoding="utf-8"))
    return {"capital_inicial": CAPITAL_INICIAL, "efectivo": CAPITAL_INICIAL, "posiciones": [],
            "predicciones": [], "cerradas": [], "comisiones_pagadas": 0.0, "ultima_ejecucion": None,
            "ultimo_resumen": None, "pausado": False, "historial_patrimonio": [], "siguiente_id": 1}


def pnl(pos, precio):
    cambio = precio / pos["precio_entrada"] - 1
    return pos["nocional"] * (cambio if pos["lado"] == "long" else -cambio)


def valor_prediccion(pred, mids):
    px = mids.get(f"#{pred['outcome'] * 10 + pred['lado_id']}")
    return pred["contratos"] * (float(px) if px else pred["precio_entrada"])


def patrimonio(c, precios, mids):
    total = c["efectivo"]
    for p in c["posiciones"]:
        total += p["margen"] + pnl(p, precios.get(p["simbolo"], p["precio_entrada"]))
    for p in c["predicciones"]:
        total += valor_prediccion(p, mids)
    return total


def nuevo_id(c):
    c["siguiente_id"] = c.get("siguiente_id", 1) + 1
    return c["siguiente_id"] - 1


def usd(x):
    s = f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-$" if x < 0 else "$") + s


def num(x):
    if x >= 1000:
        d = 2
    elif x >= 1:
        d = 4
    else:
        d = 8
    return f"{x:.{d}f}".rstrip("0").rstrip(".")


def cerrar(c, pos, precio, razon, ahora):
    bruto = pnl(pos, precio)
    comision = pos["nocional"] * (precio / pos["precio_entrada"]) * COMISION
    c["efectivo"] += pos["margen"] + bruto - comision
    c["comisiones_pagadas"] += comision
    neto = bruto - comision - pos["comision_entrada"]
    c["posiciones"].remove(pos)
    c["cerradas"].append({**pos, "precio_salida": precio, "cerrada_en": ahora.isoformat(),
                          "pnl": round(neto, 2), "razon_cierre": razon})
    return (f"Cerrado {pos['lado']} {pos['simbolo']} a {num(precio)} ({razon}): "
            f"{'+' if neto >= 0 else ''}{usd(neto)}")


# ---------- pasos ----------

def gestionar_posiciones(c, precios, cache_1h, ahora, eventos):
    for pos in list(c["posiciones"]):
        simbolo = pos["simbolo"]
        desde = pos.get("revisada_hasta") or ms(dt.datetime.fromisoformat(pos["abierta_en"]))
        tramo = velas(simbolo, "5m", desde, ms(ahora))
        salida = None
        for v in tramo:
            if pos["lado"] == "long":
                if v["l"] <= pos["stop"]:
                    salida = (min(pos["stop"], v["o"]), "stop-loss")
                elif v["h"] >= pos["objetivo"]:
                    salida = (pos["objetivo"], "objetivo")
            else:
                if v["h"] >= pos["stop"]:
                    salida = (max(pos["stop"], v["o"]), "stop-loss")
                elif v["l"] <= pos["objetivo"]:
                    salida = (pos["objetivo"], "objetivo")
            if salida:
                break
        if tramo:
            pos["revisada_hasta"] = tramo[-1]["t"]
        precio = precios.get(simbolo)
        abierta = dt.datetime.fromisoformat(pos["abierta_en"])
        if not salida and precio:
            vs = cache_1h.get(simbolo)
            if vs and tendencia_rota(vs, pos["lado"]):
                salida = (precio, "tendencia rota")
            elif ahora - abierta > dt.timedelta(hours=HORAS_MAX):
                salida = (precio, f"más de {HORAS_MAX} h abierta")
        if salida:
            eventos.append(cerrar(c, pos, salida[0], salida[1], ahora))


def buscar_entradas(c, precios, mids, volumen, cache_1h, ahora, eventos, puede_abrir):
    candidatas, sin_senal = [], []
    for simbolo, cat in CATEGORIA.items():
        if simbolo not in precios or volumen.get(simbolo, 0) < VOLUMEN_MIN:
            continue
        vs = cache_1h.get(simbolo)
        if not vs or len(vs) < 110:
            continue
        # mercado cerrado o parado: última vela vieja o sin volumen
        if ms(ahora) - vs[-1]["t"] > 2 * 3600_000 or sum(v["v"] for v in vs[-3:]) == 0:
            continue
        s = senal(vs)
        if s:
            candidatas.append((s[1], simbolo, cat, s[0], s[2]))
        else:
            sin_senal.append(simbolo)
    if not puede_abrir:
        return sin_senal

    abiertos = {p["simbolo"] for p in c["posiciones"]}
    candidatas.sort(reverse=True)
    nuevas = 0
    for fuerza, simbolo, cat, lado, a in candidatas:
        if nuevas >= MAX_NUEVAS or len(c["posiciones"]) >= MAX_POSICIONES:
            break
        if simbolo in abiertos:
            continue
        apal, stop_min, _ = MERCADOS[cat]
        precio = precios[simbolo]
        eq = patrimonio(c, precios, mids)
        distancia = max(1.5 * a / precio, stop_min)
        nocional = min(eq * RIESGO / distancia, 0.6 * eq * apal)
        nocional_total = sum(p["nocional"] for p in c["posiciones"])
        nocional = min(nocional, 3 * eq - nocional_total)
        margen = nocional / apal
        comision = nocional * COMISION
        if nocional < 50 or c["efectivo"] < margen + comision:
            continue
        if lado == "long":
            stop, objetivo = precio * (1 - distancia), precio * (1 + 2 * distancia)
        else:
            stop, objetivo = precio * (1 + distancia), precio * (1 - 2 * distancia)
        c["efectivo"] -= margen + comision
        c["comisiones_pagadas"] += comision
        c["posiciones"].append({
            "id": nuevo_id(c), "simbolo": simbolo, "mercado": cat, "lado": lado,
            "precio_entrada": precio, "nocional": round(nocional, 2), "apalancamiento": apal,
            "margen": round(margen, 2), "stop": stop, "objetivo": objetivo,
            "comision_entrada": comision, "abierta_en": ahora.isoformat(), "revisada_hasta": ms(ahora),
            "motivo": f"tendencia {lado} con momento (fuerza {fuerza:.1f} ATR)"})
        abiertos.add(simbolo)
        nuevas += 1
        eventos.append(f"Abierto {lado} {simbolo} {apal}x {usd(nocional)} a {num(precio)} "
                       f"(stop {num(stop)}, obj. {num(objetivo)})")
    return sin_senal


def describir(texto):
    campos = {}
    for parte in texto.split("|"):
        if ":" in parte:
            k, v = parte.split(":", 1)
            campos[k] = v
    return campos


def mercados_prediccion():
    """Mercados binarios de precio: 'X por encima de umbral a tal hora'."""
    salida = []
    for o in info({"type": "outcomeMeta"}).get("outcomes", []):
        if o.get("name") != "template:binaryPrice":
            continue
        d = describir(o.get("description", ""))
        try:
            vence = dt.datetime.strptime(d["time"], "%Y%m%d-%H%M").replace(tzinfo=dt.timezone.utc)
            salida.append({"outcome": o["outcome"], "subyacente": d["perp"],
                           "umbral": float(d["threshold"]), "vence": vence})
        except (KeyError, ValueError):
            continue
    return salida


def liquidar_predicciones(c, ahora, eventos):
    for pred in list(c["predicciones"]):
        vence = dt.datetime.fromisoformat(pred["vence"])
        if ahora < vence + dt.timedelta(minutes=5):
            continue
        vs = velas(pred["subyacente"], "1m", ms(vence) - 5 * 60_000, ms(vence) + 60_000)
        previas = [v for v in vs if v["t"] < ms(vence)]
        if not previas:
            if ahora - vence > dt.timedelta(days=1):
                c["efectivo"] += pred["importe"]
                c["predicciones"].remove(pred)
                eventos.append(f"Predicción {pred['titulo']} anulada (sin datos para liquidar)")
            continue
        precio_final = previas[-1]["c"]
        gana_si = precio_final > pred["umbral"]
        gana = gana_si if pred["lado_id"] == 0 else not gana_si
        cobro = pred["contratos"] if gana else 0.0
        neto = cobro - pred["importe"] - pred["comision_entrada"]
        c["efectivo"] += cobro
        c["predicciones"].remove(pred)
        c["cerradas"].append({**pred, "precio_salida": 1.0 if gana else 0.0, "cerrada_en": ahora.isoformat(),
                              "pnl": round(neto, 2),
                              "razon_cierre": f"liquidada ({num(precio_final)} vs {num(pred['umbral'])})"})
        eventos.append(f"Predicción {pred['titulo']}: {'GANADA' if gana else 'perdida'} "
                       f"{'+' if neto >= 0 else ''}{usd(neto)}")


def buscar_predicciones(c, precios, mids, cache_1h, ahora, eventos):
    if len(c["predicciones"]) >= MAX_PREDICCIONES:
        return
    ya = {p["outcome"] for p in c["predicciones"]}
    ideas = []
    for m in mercados_prediccion():
        horas = (m["vence"] - ahora).total_seconds() / 3600
        precio = precios.get(m["subyacente"])
        if m["outcome"] in ya or not precio or not 0.25 <= horas <= 48:
            continue
        vs = cache_1h.get(m["subyacente"])
        if vs is None:
            vs = velas(m["subyacente"], "1h", ms(ahora - dt.timedelta(hours=160)), ms(ahora))
            cache_1h[m["subyacente"]] = vs
        sigma = volatilidad_hora(vs)
        if not sigma:
            continue
        z = math.log(precio / m["umbral"]) / (max(sigma, 0.001) * math.sqrt(horas))
        p_si = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        for lado_id, prob in ((0, p_si), (1, 1 - p_si)):
            px = mids.get(f"#{m['outcome'] * 10 + lado_id}")
            if not px:
                continue
            px = float(px)
            if 0.05 <= px <= 0.95 and prob - px >= VENTAJA_MIN:
                ideas.append((prob - px, m, lado_id, px, prob))
    ideas.sort(key=lambda i: i[0], reverse=True)
    for _, m, lado_id, _, prob in ideas[:15]:
        if len(c["predicciones"]) >= MAX_PREDICCIONES or m["outcome"] in ya:
            continue
        importe = patrimonio(c, precios, mids) * RIESGO_PREDICCION
        comision = importe * COMISION
        if c["efectivo"] < importe + comision:
            break
        # El punto medio engaña en mercados sin actividad: se compra al mejor precio de venta del libro.
        libro = info({"type": "l2Book", "coin": f"#{m['outcome'] * 10 + lado_id}"})
        compras, ventas = (libro or {}).get("levels") or ([], [])
        if not compras or not ventas:
            continue
        mejor_compra, px = float(compras[0]["px"]), float(ventas[0]["px"])
        if px - mejor_compra > HUECO_MAX or px * float(ventas[0]["sz"]) < importe or prob - px < VENTAJA_MIN:
            continue
        nombre = m["subyacente"].replace("xyz:", "")
        titulo = (f"{nombre} {'>' if lado_id == 0 else '<='} {num(m['umbral'])} "
                  f"({m['vence']:%d-%m %H:%M} UTC)")
        c["efectivo"] -= importe + comision
        c["comisiones_pagadas"] += comision
        c["predicciones"].append({
            "id": nuevo_id(c), "outcome": m["outcome"], "lado_id": lado_id, "titulo": titulo,
            "subyacente": m["subyacente"], "umbral": m["umbral"], "vence": m["vence"].isoformat(),
            "precio_entrada": px, "importe": round(importe, 2), "contratos": importe / px,
            "comision_entrada": comision, "abierta_en": ahora.isoformat(),
            "motivo": f"modelo {prob:.0%} vs mercado {px:.0%}"})
        ya.add(m["outcome"])
        eventos.append(f"Apuesta {titulo} {usd(importe)} a {px:.0%} (modelo {prob:.0%})")


# ---------- avisos y diario ----------

def avisar(titulo, mensaje, etiquetas=("chart_with_upwards_trend",), prioridad=3):
    tema = os.environ.get("NTFY_TOPIC")
    if not tema:
        print(f"[ntfy desactivado] {titulo}: {mensaje}")
        return
    servidor = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    cuerpo = json.dumps({"topic": tema, "title": titulo, "message": mensaje,
                         "tags": list(etiquetas), "priority": prioridad}).encode()
    req = urllib.request.Request(servidor.rstrip("/"), data=cuerpo,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:
        print(f"No se pudo avisar por ntfy: {e}")


def resumen_diario(c, eq, ahora):
    hoy = ahora.date().isoformat()
    if ahora.hour < 21 or c.get("ultimo_resumen") == hoy:
        return
    c["ultimo_resumen"] = hoy
    cerradas_hoy = [x for x in c["cerradas"] if x["cerrada_en"][:10] == hoy]
    ganadas = sum(1 for x in c["cerradas"] if x["pnl"] > 0)
    lineas = [f"Patrimonio {usd(eq)} ({(eq / c['capital_inicial'] - 1):+.2%})",
              f"Hoy: {len(cerradas_hoy)} cerradas, P&L {usd(sum(x['pnl'] for x in cerradas_hoy))}",
              f"Acierto total: {ganadas}/{len(c['cerradas'])}" if c["cerradas"] else "Sin operaciones cerradas aún",
              f"Abiertas: {', '.join(p['lado'] + ' ' + p['simbolo'] for p in c['posiciones']) or 'ninguna'}"]
    if c["predicciones"]:
        lineas.append(f"Predicciones: {', '.join(p['titulo'] for p in c['predicciones'])}")
    avisar("Resumen diario del bot", "\n".join(lineas), ("bar_chart",))


def escribir_diario(ahora, eq, c, eventos, sin_senal):
    cambio = eq / c["capital_inicial"] - 1
    with DIARIO.open("a", encoding="utf-8") as f:
        if not eventos:
            f.write(f"- {ahora:%Y-%m-%d %H:%M} UTC · sin operaciones · patrimonio {usd(eq)} ({cambio:+.2%})\n")
            return
        f.write(f"\n## {ahora:%Y-%m-%d %H:%M} UTC · patrimonio {usd(eq)} ({cambio:+.2%})\n\n")
        for e in eventos:
            f.write(f"- {e}\n")
        abiertas = [f"{p['lado']} {p['simbolo']}" for p in c["posiciones"]] + [p["titulo"] for p in c["predicciones"]]
        f.write(f"- Abiertas: {', '.join(abiertas) or 'ninguna'}\n")
        if sin_senal:
            f.write(f"- Sin señal: {', '.join(sin_senal)}\n")
        f.write("\n")


# ---------- principal ----------

def main():
    ahora = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    c = cargar()
    eventos = []

    precios, volumen = contextos()
    mids = info({"type": "allMids"})

    cache_1h = {}
    desde = ms(ahora - dt.timedelta(hours=160))
    for simbolo in CATEGORIA:
        if simbolo in precios:
            try:
                cache_1h[simbolo] = velas(simbolo, "1h", desde, ms(ahora))
            except Exception as e:
                print(f"Sin velas de {simbolo}: {e}")

    gestionar_posiciones(c, precios, cache_1h, ahora, eventos)
    liquidar_predicciones(c, ahora, eventos)

    eq = patrimonio(c, precios, mids)
    puede_abrir = eq >= c["capital_inicial"] * (1 - DRAWDOWN_MAX)
    if not puede_abrir and not c.get("pausado"):
        c["pausado"] = True
        avisar("Bot en pausa", f"El patrimonio ha caído a {usd(eq)}: no abrirá operaciones nuevas.",
               ("warning",), 4)
    elif puede_abrir:
        c["pausado"] = False

    sin_senal = buscar_entradas(c, precios, mids, volumen, cache_1h, ahora, eventos, puede_abrir)
    if puede_abrir:
        buscar_predicciones(c, precios, mids, cache_1h, ahora, eventos)

    eq = patrimonio(c, precios, mids)
    c["ultima_ejecucion"] = ahora.isoformat()
    c["historial_patrimonio"] = (c.get("historial_patrimonio", []) + [[ahora.isoformat(), round(eq, 2)]])[-MAX_HISTORIAL:]
    CARTERA.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    escribir_diario(ahora, eq, c, eventos, sin_senal)

    cambio = eq / c["capital_inicial"] - 1
    if eventos:
        avisar("Bot paper trading", "\n".join(eventos) + f"\nPatrimonio {usd(eq)} ({cambio:+.2%})")
    resumen_diario(c, eq, ahora)
    CARTERA.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(eventos)} operaciones · patrimonio {usd(eq)} ({cambio:+.2%})")
    for e in eventos:
        print(" -", e)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        avisar("Error en el bot", traceback.format_exc()[-800:], ("rotating_light",), 4)
        raise
