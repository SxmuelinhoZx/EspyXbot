# Manual de operación y apagado — EspyX v15.2

> Estado: **sin validar con datos reales**. Los umbrales de win rate se llenan tras correr «Comparar» con velas reales.
> Punto de equilibrio: pago 80 % => 55,56 % · pago 85 % => 54,05 % · pago 75 % => 57,14 %.

## El bot DEBE operar solo si se cumplen TODAS
1. Cuenta **Demo** hasta que la puerta Demo→Real (espyx_gate.json) apruebe la combinación estrategia·mercado·duración.
2. «Comparar» con ≥ 20.000 velas reales por par, y el par figura **✔ Confirmada** (supera el equilibrio en selección Y en prueba final).
3. Pago real del activo ≥ 80 % («Pago mínimo»).
4. Modo «Solo los que superan el equilibrio» (nunca «Todos los elegidos» en Real).
5. Filtro de tendencia en «Obligatorio»; filtro de noticias encendido (solo alto impacto, 15 min antes / 15 después).
6. Ventana horaria de mayor liquidez (solape Londres–Nueva York, aprox. 08:00–12:00 hora de Nueva York); fuera de ella, no operar.
7. Gestión: monto fijo o interés compuesto por par (1 reinversión). **Martingala desactivada.**
8. Monto por operación ≤ 1 % del capital total.

## El bot DEBE pausarse / apagarse si ocurre CUALQUIERA
- Noticia de alto impacto de la divisa del par (el filtro frena solo; no lo desactives).
- Apertura de mercado (primeros 15 min de Londres y Nueva York) y cierre de sesión / fin de semana (solo OTC, que no sigue al mercado real).
- 3 pérdidas seguidas (protección por defecto) → parar el día.
- Pérdida del día ≥ 3 % del capital → parar el día. Pérdida acumulada ≥ 10 % → parar la semana y revisar.
- El vigilante de salud pausa el par 60 min (efectividad en vivo bajo el equilibrio).
- Efectividad en vivo < equilibrio con ≥ 50 operaciones por combinación → desactivarla y volver a «Comparar».
- Mercado lateral estrecho (régimen «lateral» con ER bajo) con estrategias de tendencia, y volatilidad fuera del rango del backtest.
- Entrada fuera de tiempo (pasó el «Entrar como máximo al segundo» 8) → no se abre; no forzar.
- Pago del activo baja de 80 % o el par aparece cerrado.
- Cambio de comportamiento: repetir «Comparar» al menos cada semana; si la combinación deja de ser ✔ Confirmada, no operarla.

## Reporte cuantitativo (pendiente de datos reales)
| Métrica | Valor |
|---|---|
| Operaciones simuladas (prueba final) | pendiente |
| Win rate exacto + límite inferior Wilson 95 % | pendiente |
| Racha ganadora / perdedora máxima | pendiente |
| Drawdown máximo | pendiente |
| Payout mínimo requerido | 80 % (equilibrio 55,56 %) |
