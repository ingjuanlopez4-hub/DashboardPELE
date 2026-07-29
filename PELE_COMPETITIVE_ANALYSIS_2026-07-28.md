# Analisis competitivo UX de PELE

**Producto:** PELE, radar publico de senales para mercados de Polymarket<br>
**Mercado prioritario:** Latinoamerica<br>
**Corte:** 28 de julio de 2026<br>
**Superficie verificada:** `http://127.0.0.1:3000/`<br>
**Metodo:** recorrido funcional en escritorio y movil, evaluacion heuristica y contraste con superficies y documentacion publica de competidores.

## 1. Resumen ejecutivo

PELE ocupa un espacio defendible entre el exchange y la terminal profesional: ayuda a responder **que mercado merece atencion, cuanta confianza merece su lectura y de donde sale cada dato** antes de abrir Polymarket.

Su ventaja no es tener mas mercados ni ejecutar ordenes. Es sintetizar probabilidad, cambio, actividad, liquidez y solidez en una lectura explicable, sin login ni wallet, con una experiencia en espanol. Ningun competidor revisado combina actualmente esas tres cualidades.

La experiencia actual alcanza **3,5/5**. La propuesta, los filtros, la priorizacion y la procedencia son fuertes. Los principales riesgos son operativos:

1. Mercados vencidos o con apariencia desactualizada conviven con la etiqueta de mercados abiertos.
2. La media simple Si/No de contratos heterogeneos recibe una prominencia que puede confundirse con consenso.
3. En movil, el panel comparativo aparece muy por encima del mercado que completa la seleccion.
4. Vigilancia y simulacion estan enterradas despues de una lista extensa.
5. Valores como `-0 %`, probabilidades extremas y solidez alta con datos faltantes reducen confianza.

### Tesis recomendada

> **PELE debe ser la capa publica y explicable que ayuda a decidir que probabilidad merece confianza antes de abrir el exchange.**

Esto evita competir frontalmente con Polymarket, Kalshi o Limitless en ejecucion, y con Polymarket Analytics solo en inteligencia de wallets. PELE puede apropiarse de la interseccion entre deteccion de anomalias, calidad de senal, comparacion y contexto regional en espanol.

## 2. Competidores seleccionados

| Producto | Tipo | Por que importa |
|---|---|---|
| Polymarket | Directo / fuente primaria | Referente de inventario, liquidez, contexto y ejecucion del mercado de origen |
| Kalshi | Directo | Referente de reglas, fuentes de verificacion y arquitectura de contratos regulados |
| Limitless | Directo | Referente cripto nativo, mercados recurrentes y transparencia de oraculos |
| Polymarket Analytics | Directo / capa de inteligencia | Referente de wallets, PnL, open interest y seguimiento de traders |
| Manifold | Indirecto | Referente de contexto social, argumentos y aprendizaje sin riesgo monetario |
| TradingView | Aspiracional | Referente del flujo screener, comparacion, alerta, escenario y ejecucion |

## 3. Evaluacion de PELE

| Dimension | Nota | Evidencia principal |
|---|---:|---|
| Propuesta de valor | 4/5 | Explica identificar, comparar y verificar; el titular editorial conserva personalidad |
| Arquitectura de informacion | 3/5 | Progresion coherente, pero vigilancia y simulacion quedan tras una lista muy larga |
| Busqueda y filtros | 4/5 | Busca, filtra, ordena y recupera desde vacio con feedback claro |
| Priorizacion de senales | 4/5 | Intensidad, cambio, solidez y razon dominante facilitan el escaneo |
| Comparacion | 3/5 | Limite de tres y estados correctos; panel basico y desconectado en movil |
| Expediente y explicabilidad | 4/5 | Separa observacion, derivado y modelo; la jerga exige apoyo contextual |
| Vigilancia | 3/5 | Activacion y persistencia local claras; descubrimiento debil en movil |
| Simulacion | 4/5 | Estados, rango y procedencia utiles; admite reenvio accidental mientras carga |
| Handoff a Polymarket | 3/5 | Destino correcto y seguro; el control visible `↗` comunica poco para una salida financiera |
| Diseno visual | 4/5 | Identidad de sismografo distintiva, jerarquia fuerte y color funcional |
| Movil | 3/5 | Sin overflow, pero hero y lista alargan excesivamente las tareas recurrentes |
| Accesibilidad | 3,5/5 | Semantica, foco, labels y movimiento reducido correctos; quedan brechas menores |
| Confianza | 3/5 | Procedencia excelente; frescura, ciclo de vida y valores limite necesitan normalizacion |

## 4. Matriz de tareas competitiva

**Formato:** soporte `C` completo, `P` parcial, `N` no verificado; pasos aproximados desde la entrada; calidad UX de 1 a 5.

### Descubrimiento y analisis

| Tarea | PELE | Polymarket | Kalshi | Limitless | Poly. Analytics | Manifold | TradingView |
|---|---|---|---|---|---|---|---|
| Descubrir y filtrar | `C · 2 · 4` filtros cuantitativos | `C · 2 · 4` categorias, search, tendencias | `C · 2 · 4` temas y trending | `C · 2 · 3` categorias y lineas recurrentes | `C · 2 · 4` OI, mercados y traders | `C · 2 · 4` Best/Hot/New | `C · 3 · 5` screeners configurables |
| Interpretar la senal | `C · 1 · 4` intensidad, solidez y razon | `P · 2 · 3` precio, volumen, libro y noticias | `C · 2 · 4` forecast y datos externos | `P · 2 · 3` oraculo, velas y libro | `C · 2 · 4` PnL, wallets y OI | `C · 2 · 4` argumentos y reputacion | `C · 2 · 5` indicadores y contexto |
| Entender el contrato | `P · 2 · 3` expediente, luego origen | `C · 1 · 4` reglas y resolucion | `C · 1 · 5` fuente, reglas y payout | `C · 1 · 4` oraculo y casos limite | `P · 2 · 3` domina la analitica | `P · 1 · 3` resolucion del creador | `C · 2 · 4` metadatos, no resolucion binaria |
| Comparar mercados | `C · 2 · 3` fija hasta tres | `P · 2 · 3` outcomes relacionados | `P · 2 · 3` escaleras de strikes | `P · 2 · 3` outcomes y packs | `P · 2 · 3` tablas y rankings | `P · 3 · 3` sin espacio formal | `C · 2 · 5` overlays y multigrafico |
| Explicar procedencia | `C · 1 · 5` observado, derivado y modelo | `C · 1 · 4` mercado y resolucion | `C · 1 · 5` fuente verificadora | `C · 1 · 4` oraculo y lifecycle | `P · 2 · 3` dependencia de terceros | `P · 1 · 3` creador y comunidad | `C · 2 · 4` exchange y fuentes |

### Continuidad y accion

| Tarea | PELE | Polymarket | Kalshi | Limitless | Poly. Analytics | Manifold | TradingView |
|---|---|---|---|---|---|---|---|
| Vigilar o alertar | `P · 3 · 3` local y por mercado | `P · 2 · 2` actividad/portfolio | `P · 2 · 2` portfolio/auto-sell | `N · - · 1` no verificado | `C/P · 2 · 4` watchlist y Telegram | `P · 2 · 3` follow/notificaciones | `C · 2 · 5` multicondicion y webhook |
| Simular escenarios | `C · 3 · 4` GBM externo | `P · 1 · 3` payout binario | `P · 1 · 3` payout/hedging | `P · 1 · 3` payout y PnL | `P · 2 · 3` historico | `P · 1 · 2` mana | `C · 2 · 5` strategy tester y what-if |
| Continuidad movil | `P · 1 · 3` web responsive | `C · 1 · 4` web + iPhone | `C · 1 · 5` iOS + Android | `P · 1 · 3` web responsive | `C/P · 1 · 4` web + Telegram | `C/P · 1 · 4` app referenciada | `C · 1 · 5` web, desktop y apps |
| Ejecutar o derivar | `C · 1 · 3` enlace externo | `C · 1 · 5` ticket nativo | `C · 1 · 5` quick/limit order | `C · 2 · 4` wallet y orden | `C/P · 2 · 4` enlace o PolyGun | `P · 1 · 2` dinero de juego | `C · 2 · 5` broker o paper trading |
| Acceso sin cuenta | `C · 0 · 5` lectura publica | `C · 0 · 4` investigacion publica | `P · 0 · 3` profundidad condicionada | `P · 0 · 3` wallet para actuar | `P · 0 · 3` watchlist autenticada | `C · 0 · 4` exploracion publica | `P · 0 · 3` varias funciones requieren cuenta |

## 5. Perfiles competitivos

### Polymarket

**Fortalezas:** amplitud, liquidez, agrupacion de outcomes, noticias, comentarios, reglas y ejecucion en una misma ficha.<br>
**Debilidad frente a PELE:** muestra datos, pero explica poco por que un movimiento es anomalo o si la probabilidad es robusta.<br>
**Patron transferible:** incluir reglas, fuente de resolucion, estado y precio vigente antes del salto.<br>
**Riesgo regional:** Brasil, Nicaragua y Venezuela figuran como close-only; Cuba esta bloqueada.

### Kalshi

**Fortalezas:** mejor contexto contractual del grupo; fuente verificadora, reglas, timeline, payout y graficos de forecast.<br>
**Debilidad frente a PELE:** acceso internacional, KYC y fondeo introducen friccion; la experiencia verificada sigue siendo ingles-first.<br>
**Patron transferible:** hacer de la fuente de verdad y del ciclo de vida elementos de primer nivel.

### Limitless

**Fortalezas:** mercados cortos y recurrentes, oraculos visibles, lifecycle y ordenes cripto nativas.<br>
**Debilidad frente a PELE:** wallet, USDC y Base elevan el coste de entrada; no se verifico un workspace fuerte de watchlists o comparacion.<br>
**Patron transferible:** mostrar fuente del oraculo, deadline y casos limite en lenguaje operativo.

### Polymarket Analytics

**Fortalezas:** traders, PnL, posiciones, open interest, watchlists y continuidad mediante Telegram.<br>
**Debilidad frente a PELE:** prioriza quien mueve el mercado sobre la calidad intrinseca del contrato; la comparacion lateral es limitada.<br>
**Patron transferible:** incorporar actividad de wallets y OI como evidencia, no como sustituto de la senal.

### Manifold

**Fortalezas:** argumentos, fuentes, comentarios, reputacion y aprendizaje sin riesgo financiero.<br>
**Debilidad frente a PELE:** resolucion variable por creador y sin dinero convertible.<br>
**Patron transferible:** unir cada cambio de probabilidad con explicaciones y evidencia comunitaria relevante.

### TradingView

**Fortalezas:** ciclo completo de screener, vista guardada, comparacion, alerta, escenario y ejecucion; continuidad multidispositivo y objetivo WCAG 2.2 AA publicado.<br>
**Debilidad como patron directo:** su densidad y complejidad no son adecuadas para copiar sin adaptacion.<br>
**Patron transferible:** bandeja de seleccion persistente, vistas guardadas, alertas reutilizables y comparacion sin perder contexto.

## 6. Table stakes

- Busqueda y navegacion estable por categoria.
- Filtros por actividad, volumen/OI, liquidez, probabilidad y cierre.
- Historial de probabilidad con periodo y frescura explicitos.
- Precio, bid/ask o spread, liquidez y volumen con unidades claras.
- Reglas, fuente de resolucion, deadline, estado y casos limite.
- Outcomes relacionados agrupados bajo un evento.
- Watchlists persistentes y alertas de movimiento, volumen, cierre y resolucion.
- Deep links que preserven mercado, outcome y contexto.
- Timestamp, latencia y procedencia junto a cada lectura.
- Estados no dependientes solo del color, teclado, foco y movimiento reducido.
- Handoff seguro con destino, outcome, precio, elegibilidad y advertencia de frescura.

## 7. Gaps del mercado

1. **Anomalias explicables:** los productos muestran actividad, pero rara vez explican por que es inusual respecto al propio contrato.
2. **Confianza de senal:** falta separar evidencia fuerte de ruido por baja liquidez, precio obsoleto o concentracion de wallets.
3. **Comparacion event-aware:** ningun especialista ofrece una comparacion equivalente a TradingView entre contratos relacionados.
4. **Riesgo de resolucion:** ambiguedad, cambio de criterios y riesgo de oraculo no se elevan como senales principales.
5. **Alertas profesionales:** faltan reglas reutilizables, severidad, quiet hours, historial y deduplicacion.
6. **Investigacion publica portable:** las vistas compartibles y read-only suelen quedar detras de una cuenta.
7. **Experiencia analitica en espanol:** no se encontro un especialista maduro y Spanish-first para mercados predictivos.
8. **Responsabilidad accesible:** solo TradingView publica un objetivo de accesibilidad sustantivo.

## 8. Oportunidades priorizadas

Escala: impacto y confianza de 1 a 5; esfuerzo inverso de 1 a 5, donde 5 es menor esfuerzo.

| Rank | Oportunidad | Impacto | Confianza | Esfuerzo inverso | Total | Horizonte |
|---:|---|---:|---:|---:|---:|---|
| 1 | Mostrar estado de ciclo de vida y edad del dato en cada tarjeta | 5 | 5 | 4 | **14** | Inmediato |
| 2 | Normalizar `+0/-0`, faltantes y extremos; separar solidez de probabilidad | 5 | 5 | 5 | **15** | Inmediato |
| 3 | Demover la media Si/No o sustituirla por cobertura/anomalias activas | 5 | 5 | 4 | **14** | Inmediato |
| 4 | Crear bandeja sticky `N seleccionados · Comparar` en movil | 5 | 5 | 3 | **13** | Proximo ciclo |
| 5 | Resumir la comparacion con lider, divergencia y razon dominante | 4 | 5 | 3 | **12** | Proximo ciclo |
| 6 | Exponer reglas, fuente, deadline y elegibilidad antes del handoff | 5 | 4 | 2 | **11** | Proximo ciclo |
| 7 | Convertir vigilancia en alertas por condiciones con historial | 5 | 4 | 2 | **11** | Estrategico |
| 8 | Persistir y compartir filtros, selecciones y vistas por URL | 4 | 4 | 3 | **11** | Proximo ciclo |
| 9 | Incorporar OI, profundidad y concentracion de wallets cuando existan | 5 | 4 | 2 | **11** | Estrategico |
| 10 | Localizar deadlines y contexto regional preservando zona fuente | 4 | 4 | 3 | **11** | Proximo ciclo |

### Now: confianza operativa

- Separar abiertos, cerrados, en resolucion y datos obsoletos.
- Mostrar edad de la ultima observacion.
- Sustituir cambios cercanos a cero por "Sin cambio material".
- Explicar que solidez mide calidad de evidencia, no probabilidad de que ocurra el outcome.
- Reducir la autoridad visual de la media simple de la muestra.

### Next: velocidad y continuidad

- Bandeja sticky de comparacion y acceso directo tras fijar el segundo mercado.
- Resumen comparativo que interprete diferencias, no solo repita cifras.
- Filtros y selecciones persistentes y compartibles.
- Acceso movil visible a vigilancia sin abrir primero el expediente.
- Handoff etiquetado como "Abrir en Polymarket" con outcome, frescura y elegibilidad.

### Later: ventaja estrategica

- Alertas multicondicion con severidad, quiet hours e historial.
- Riesgo de resolucion como score separado.
- Actividad de wallets y profundidad como evidencia explicable.
- Historial temporal de intensidad y solidez.
- Fuentes regionales y vocabulario especializado para Latinoamerica.

## 9. Consideraciones para Latinoamerica

- Mantener una experiencia completa en espanol, no una traduccion literal del ingles financiero.
- Mostrar hora local y conservar siempre UTC/ET de la fuente para evitar errores de cierre.
- Usar formatos monetarios y decimales explicitos; no asumir familiaridad con centavos de contrato.
- Avisar que disponibilidad publica no equivale a elegibilidad legal o de ejecucion.
- Tratar wallet, red, bridge, gas y custodia como riesgos del handoff a productos cripto.
- Priorizar web responsive y bajo consumo de datos; Android y Telegram tienen relevancia regional.
- Incorporar fuentes locales autorizadas para elecciones, bancos centrales, commodities y futbol.

## 10. Referencias anotadas

Consultadas el 28 de julio de 2026. Las capacidades autenticadas o de ejecucion no se probaron con fondos reales.

### Evidencia de PELE

- `competitive-analysis-current-desktop.png`: pagina completa actual en escritorio.
- `competitive-analysis-current-mobile.png`: pagina completa actual a 390 x 844.
- `PRODUCT.md`: usuarios, proposito, restricciones y principios del producto.
- `DESIGN.md`: norte creativo, tokens y reglas de interfaz.
- `PELE_COMPETITIVE_AUDIT.md`: linea base del 27 de julio; no se asumieron vigentes sus conclusiones.
- `README.md`: arquitectura funcional y separacion entre radar publico y bot.

### Polymarket

- [Interfaz publica](https://polymarket.com/): descubrimiento, ficha y ejecucion.
- [Discover Markets](https://docs.polymarket.com/market-data/discover-markets): eventos, mercados, series, tags y busqueda.
- [Public Analytics](https://docs.polymarket.com/market-data/public-analytics): fuentes analiticas publicas.
- [Geographic Restrictions](https://docs.polymarket.com/api-reference/geoblock): restricciones regionales que afectan el handoff.
- [App oficial para iPhone](https://apps.apple.com/us/app/polymarket/id6648798962): soporte nativo y declaracion de idioma/accesibilidad.

### Kalshi

- [Finding Markets](https://help.kalshi.com/en/articles/13823842-finding-markets): journey de descubrimiento.
- [Market Rules](https://help.kalshi.com/en/articles/13823822-market-rules): reglas y fuentes verificadoras.
- [Forecast Graph](https://help.kalshi.com/en/articles/13823829-forecast-graph): agregacion de expectativas entre strikes.
- [International Access](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states): elegibilidad y fondeo internacional.
- [Android](https://play.google.com/store/apps/details?id=com.kalshi.mobile): presencia movil regionalmente relevante.

### Limitless

- [Interfaz publica](https://limitless.exchange/): inventario, categorias y experiencia responsive.
- [Making Your First Trade](https://docs.limitless.exchange/user-guide/making-your-first-trade): wallet, Base y USDC.
- [Market Resolution](https://docs.limitless.exchange/user-guide/market-resolution): oraculos, resolucion y casos limite.
- [Terms](https://docs.limitless.exchange/user-guide/terms-of-service): restricciones y responsabilidad jurisdiccional.

### Polymarket Analytics

- [Markets](https://polymarketanalytics.com/markets): screening por mercado y open interest.
- [Traders](https://polymarketanalytics.com/traders): inteligencia de wallets y rendimiento.
- [Watchlist](https://polymarketanalytics.com/watchlist): continuidad autenticada.
- [PolyGun](https://polymarketanalytics.com/copy-trade): handoff y copy trading mediante Telegram.
- [Terms](https://polymarketanalytics.com/terms): latencia, terceros y limites de precision.

### Manifold

- [Browse](https://manifold.markets/browse): descubrimiento social.
- [About](https://manifold.markets/about): propuesta y moneda de juego.
- [FAQ](https://docs.manifold.markets/faq): mecanica, liquidez y resolucion.
- [API](https://docs.manifold.markets/api): filtros y ordenacion disponibles.

### TradingView

- [Screener Walkthrough](https://www.tradingview.com/support/solutions/43000718885-tradingview-screeners-walkthrough/): filtros, columnas y vistas guardadas.
- [Alerts](https://www.tradingview.com/support/solutions/43000520149-introduction-to-tradingview-alerts/): condiciones y canales.
- [Mobile](https://www.tradingview.com/mobile/): continuidad multidispositivo.
- [Trading](https://www.tradingview.com/trading/): brokers y paper trading.
- [Accessibility](https://www.tradingview.com/accessibility/): objetivo WCAG 2.2 AA y alcance declarado.

## 11. Limitaciones

- No se crearon cuentas, completaron KYC, financiaron wallets ni ejecutaron ordenes.
- Watchlists, portfolios y notificaciones autenticadas se evaluaron mediante documentacion publica.
- La accesibilidad competitiva es una indicacion, no una auditoria WCAG completa.
- No se probaron fallo de API, offline, slow 3G, zoom ni lector de pantalla en PELE durante este corte.
- Restricciones, inventario, terminos y aplicaciones cambian rapidamente; revalidar trimestralmente y antes de decisiones de lanzamiento.
