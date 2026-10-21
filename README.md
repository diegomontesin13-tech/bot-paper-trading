# Bot de paper trading

Bot que cada 15 minutos analiza los mercados de Hyperliquid (los mismos que usa Liquid) y opera una **cartera 100% simulada** de $10.000. No usa claves ni envía órdenes: solo lee datos públicos de `https://api.hyperliquid.xyz/info`. Funciona en GitHub Actions, así que no hace falta tener el ordenador encendido.

## Qué opera

| Mercado | Ejemplos | Apalancamiento |
|---|---|---|
| Cripto | BTC, ETH, SOL, HYPE, XRP, BNB | hasta 3x |
| Memecoins | DOGE, kPEPE, kSHIB, kBONK, WIF, POPCAT, PENGU, FARTCOIN, TRUMP | 1x |
| Acciones | NVDA, TSLA, AAPL, MSFT, META, GOOGL, AMZN, AMD, PLTR, COIN, MSTR | hasta 3x |
| Índices | S&P 500, Nasdaq 100, Nikkei 225, KOSPI 200 | hasta 3x |
| Forex | EURUSD, USDJPY, DXY | hasta 3x |
| Materias primas | oro, plata, petróleo WTI y Brent, gas natural, cobre | hasta 3x |
| Predicciones | mercados HIP-4 "precio por encima de X a tal hora" | sin apalancamiento |

## Reglas

- Riesgo del 1% del patrimonio por operación, siempre con stop-loss (1,5 ATR de 1h, con un mínimo por mercado) y objetivo a 2 veces el stop.
- Entra solo si tendencia y momento coinciden en velas de 1h (precio > EMA20 > EMA50 > EMA100, EMA20 subiendo y RSI 50-70 para largos; al revés para cortos).
- Cierra por stop, objetivo, tendencia rota (precio cruza la EMA50) o tras 48 h. Los stops se revisan con velas de 5 minutos y, si en la misma vela se tocan stop y objetivo, cuenta como stop.
- Máximo 5 posiciones, 2 aperturas por ejecución y nocional total ≤ 3× patrimonio. Comisión simulada del 0,05% por lado.
- Predicciones: apuesta el 0,5% del patrimonio solo si su modelo (volatilidad reciente) ve al menos 10 puntos más de probabilidad que el mercado. Máximo 3 abiertas.
- Si el patrimonio cae un 15%, deja de abrir operaciones y avisa.

## Archivos

- `bot.py`: el bot (solo librería estándar de Python).
- `cartera.json`: saldo, posiciones, predicciones, operaciones cerradas e historial del patrimonio.
- `diario.md`: lo que hizo en cada ejecución.
- `.github/workflows/bot.yml`: lo ejecuta cada 15 minutos y guarda los cambios con un commit.

## Notificaciones (ntfy)

1. Instala la app ntfy (Android/iOS) o abre https://ntfy.sh y suscríbete a tu tema.
2. En GitHub: Settings → Secrets and variables → Actions → New repository secret, nombre `NTFY_TOPIC`, valor: el nombre del tema.
3. (Opcional) Si usas tu propio servidor ntfy, crea la variable `NTFY_SERVER` con su URL.

Avisa cuando abre o cierra algo, cuando entra en pausa, si hay un error y con un resumen diario a partir de las 21:00 UTC.

## Limitaciones

- GitHub no garantiza la hora exacta de los workflows programados: suelen llegar con unos minutos de retraso y a veces se salta alguno.
- Los precios de entrada y salida son los del momento de la ejecución; en real habría deslizamiento y quizá menos liquidez, sobre todo en predicciones.
- Las predicciones se liquidan con el precio del perpetuo en el vencimiento, que puede diferir algo del oráculo oficial.
- Es un experimento: los resultados simulados no son una recomendación de inversión.
