"""El agente: catálogo de tools, presupuesto, permisos y traza.

Importar este paquete da de alta las tools. Es deliberado que sea un efecto del
import y no una llamada explícita: si dar de alta una tool requiriera acordarse
de invocar algo, tarde o temprano alguien agrega un archivo de tools que nunca
se registra y el síntoma es "el modelo no la usa nunca", que es carísimo de
diagnosticar.
"""
from . import tools_alerts, tools_cost, tools_metrics  # noqa: F401 — el import ES el registro
from .loop import Result, Step, run
from .registry import Context, definitions, execute, known

__all__ = ["Context", "Result", "Step", "definitions", "execute", "known", "run"]
