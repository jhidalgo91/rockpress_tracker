# Estrategia de Fetch Optimizada – RockPress Tracker

## Problema resuelto

El rastreo inicial usaba las **páginas de inicio** de cada medio, que pueden superar los 50-80KB en una sola línea de HTML comprimido o JSON. Esto hacía imposible el chunking con el tool `Read` y provocaba pérdida de noticias (caso DRAGONFLY, 19/06/2026).

---

## Solución: Páginas de Archivo Diario (WordPress)

Para medios basados en WordPress, usar la URL de archivo por fecha:

```
https://DOMINIO.com/YYYY/MM/DD/
```

### Ventajas
- Devuelve **solo los artículos de ese día** en HTML limpio
- Tamaño manejable (< 30KB normalmente)
- No requiere chunking; se lee en una sola llamada
- Incluye título, enlace, resumen y fecha visibles

### Test realizado (19/06/2026)
| Sitio | URL de archivo | Resultado |
|---|---|---|
| Metalcry | https://metalcry.com/2026/06/19/ | ✅ Funciona — 16 artículos del día |
| Mariskalrock | https://mariskalrock.com/2026/06/19/ | ❌ Redirige a homepage |

---

## Protocolo de ejecución optimizado

Para cada día del margen (D-3, D-2, D-1, D), por cada medio WordPress:

1. Fetch `https://SITIO.com/YYYY/MM/DD/` para cada uno de los 3 días
2. Si la URL funciona: extraer artículos directamente
3. Si redirige a homepage: hacer fetch de la homepage y leer primeras 200 líneas

Para medios con **RSS funcional** (no gzip):
- Hacer fetch del feed y leer completo

Para medios sin WordPress ni RSS:
- Fetch de homepage + leer primeras 200 líneas

---

## Sitios WordPress confirmados (archive pages funcionan)

- ✅ **Metalcry** — `https://metalcry.com/YYYY/MM/DD/`
- ✅ **Ballesterock** — `https://ballesterockmusic.com/YYYY/MM/DD/`
- ✅ **TNT Radio Rock** — `https://tntradiorock.com/YYYY/MM/DD/`
- ✅ **Noche de Rock** — `https://www.nochederock.com/YYYY/MM/DD/`
- ✅ **Rock Sesión** — `https://rocksesion.com/YYYY/MM/DD/`
- ✅ **The Sentinel** — `https://www.thesentinel.es/wpsentinel/YYYY/MM/DD/`
- ❌ **Mariskalrock** — NO funciona (Elementor/custom theme, redirige a home)

---

## Template de URLs por día (ejemplo 19-21 junio 2026)

```
Metalcry:
  https://metalcry.com/2026/06/19/
  https://metalcry.com/2026/06/18/
  https://metalcry.com/2026/06/17/

Ballesterock:
  https://ballesterockmusic.com/2026/06/19/
  https://ballesterockmusic.com/2026/06/18/
  https://ballesterockmusic.com/2026/06/17/

TNT Radio Rock:
  https://tntradiorock.com/2026/06/19/
  https://tntradiorock.com/2026/06/18/
  https://tntradiorock.com/2026/06/17/

Noche de Rock:
  https://www.nochederock.com/2026/06/19/
  https://www.nochederock.com/2026/06/18/
  https://www.nochederock.com/2026/06/17/
```

---

## Noticias recuperadas gracias a esta optimización (test 19/06/2026)

Artículos que la estrategia anterior (homepage parcial) había perdido:

| Titular | Relevancia |
|---|---|
| MIGUEL RÍOS cancela conciertos por traumatismo craneal | ⭐⭐⭐⭐⭐ |
| WHISKEY VALENTINE — nuevo proyecto de Fran Vázquez | ⭐⭐⭐⭐ |
| GRANITOROCK 2026 — REPRISE y HOMESICK en cartel | ⭐⭐⭐ |
| MOVE YOUR F*CKING BRAIN EXTREME FEST 20ª edición (Barcelona) | ⭐⭐⭐ |

---

*Documento generado el 19/06/2026 tras test de optimización.*
