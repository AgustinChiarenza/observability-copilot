"""Copilot — addon de observabilidad.

Se enchufa al stack que el cliente ya tiene: lee sus métricas por PromQL, recibe
las alertas que su Alertmanager ya evalúa y consulta el costo de donde ya esté.
No trae TSDB, no trae motor de reglas y no toca infraestructura.

Regla de arquitectura, en una línea: **fuera de `copilot/adapters/` no se
importa el SDK de ningún proveedor.** Todo lo demás se deriva de eso.
"""
__version__ = "0.1.0"
