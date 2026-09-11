"""Configuración: un YAML que el cliente puede commitear, y secretos aparte.

Dos reglas que fijan la forma del archivo:

1. **El YAML no contiene ni un secreto.** Cualquier valor puede escribirse como
   `${VAR}` o `${VAR:-default}` y se resuelve contra el entorno al cargar. Así el
   cliente versiona su `copilot.yaml` en su repo de infra —que es donde quiere
   tenerlo, revisado por PR— sin que nadie tenga que acordarse de tacharlo.
   `${VAR}` sin valor y sin default es un error de arranque, no un string vacío
   que se descubre tres horas después como un 401 raro.

2. **Se valida al arrancar y se corta.** Un adapter mal escrito, una URL que
   falta o un canal duplicado tienen que matar el proceso ahora, con el nombre
   del campo, y no convertirse en la alerta que no llegó el sábado. Es la misma
   lógica que `config_check` en el proyecto anterior: un server que no puede
   avisar no está degradado, está mudo.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import yaml

from .detectors.cost_spike import SpikeConfig
from .dispatch import Policy, QuietHours
from .ports.metrics import Budget

DEFAULT_PATH = os.getenv("COPILOT_CONFIG", "/etc/copilot/copilot.yaml")


class ConfigError(RuntimeError):
    """La configuración no permite arrancar. El mensaje nombra el campo."""


# ${VAR} | ${VAR:-default}
_RE_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_RE_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)\s*$")

_DURATION_UNIT = {
    "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0,
}


def expand(value: Any, *, where: str = "") -> Any:
    """Resuelve `${VAR}` en strings, recursivamente en dicts y listas."""
    if isinstance(value, dict):
        return {k: expand(v, where=f"{where}.{k}" if where else k) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, where=f"{where}[{i}]") for i, v in enumerate(value)]
    if not isinstance(value, str):
        return value

    def _sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        env = os.getenv(name)
        if env is not None and env != "":
            return env
        if default is not None:
            return default
        raise ConfigError(
            f"{where or 'config'}: la variable de entorno {name} no está definida "
            f"y no tiene default. Definila, o escribí ${{{name}:-valor}}."
        )

    return _RE_ENV.sub(_sub, value)


def parse_duration(raw: Any, *, where: str) -> timedelta:
    """`30s`, `5m`, `31d` → timedelta. Un número pelado se lee como segundos."""
    if isinstance(raw, (int, float)):
        return timedelta(seconds=float(raw))
    m = _RE_DURATION.match(str(raw))
    if not m:
        raise ConfigError(
            f"{where}: '{raw}' no es una duración. Usá un número con unidad "
            f"(ms, s, m, h, d, w), por ejemplo '30s' o '31d'."
        )
    return timedelta(seconds=float(m.group(1)) * _DURATION_UNIT[m.group(2)])


@dataclass
class AdapterConfig:
    """Qué adapter y con qué opciones. `options` va crudo al adapter.

    El core no valida `options`: cada adapter conoce las suyas y las valida en su
    propio constructor. Centralizar esa validación acá obligaría a tocar el core
    para agregar un adapter, que es exactamente lo que este diseño evita.
    """

    adapter: str
    options: dict[str, Any] = field(default_factory=dict)
    name: str = ""

    @classmethod
    def parse(cls, raw: dict[str, Any], *, where: str) -> AdapterConfig:
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: se esperaba un bloque, vino {type(raw).__name__}.")
        adapter = str(raw.get("adapter") or "").strip()
        if not adapter:
            raise ConfigError(f"{where}.adapter: falta. Mirá `copilot adapters` para la lista.")
        options = {k: v for k, v in raw.items() if k not in ("adapter", "name")}
        return cls(adapter=adapter, options=options, name=str(raw.get("name") or adapter))


@dataclass
class AgentConfig:
    """Presupuesto del loop.

    Los topes no están para ahorrar tokens sino para que un modelo enroscado
    pidiendo la misma tool no queme la sesión. Un pedido que necesita diez
    consultas tiene que poder hacerlas.
    """

    max_steps: int = 12
    max_seconds: float = 180.0
    temperature: float = 0.2
    system_prompt: str = ""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"       # noqa: S104 — corre en un contenedor, el borde lo pone el orquestador
    port: int = 8080
    #: Token para las rutas que no son de salud. Vacío = sin auth, y sólo se
    #: tolera si `allow_insecure` está prendido.
    api_token: str = ""
    allow_insecure: bool = False


@dataclass
class DetectorsConfig:
    cost_spike: SpikeConfig = field(default_factory=SpikeConfig)


def _parse_hhmm(raw: Any, *, where: str) -> time:
    try:
        h, m = str(raw).split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError) as e:
        raise ConfigError(f"{where}: '{raw}' no es una hora. Usá HH:MM, por ejemplo 22:00.") from e


def _parse_dispatch(raw: dict[str, Any]) -> Policy:
    qh = raw.get("quiet_hours") or None
    quiet = None
    if qh:
        if not isinstance(qh, dict) or "start" not in qh or "end" not in qh:
            raise ConfigError("dispatch.quiet_hours: necesita `start` y `end` (HH:MM).")
        tz = str(qh.get("tz", "UTC"))
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception as e:
            raise ConfigError(f"dispatch.quiet_hours.tz: '{tz}' no es una zona horaria "
                              f"válida (ej: America/Argentina/Buenos_Aires).") from e
        quiet = QuietHours(
            start=_parse_hhmm(qh["start"], where="dispatch.quiet_hours.start"),
            end=_parse_hhmm(qh["end"], where="dispatch.quiet_hours.end"),
            tz=tz,
        )
    return Policy(
        repeat_interval=parse_duration(
            raw.get("repeat_interval", "4h"), where="dispatch.repeat_interval"),
        quiet_hours=quiet,
        daily_cap=int(raw.get("daily_cap", 50)),
    )


def _parse_detectors(raw: dict[str, Any]) -> DetectorsConfig:
    cs = raw.get("cost_spike") or {}
    return DetectorsConfig(cost_spike=SpikeConfig(
        enabled=bool(cs.get("enabled", False)),
        interval=parse_duration(cs.get("interval", "1h"), where="detectors.cost_spike.interval"),
        window_days=int(cs.get("window_days", 14)),
        threshold=float(cs.get("threshold", 1.5)),
        min_amount=float(cs.get("min_amount", 0)),
        min_days=int(cs.get("min_days", 5)),
    ))


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    metrics: AdapterConfig | None = None
    cost: AdapterConfig | None = None
    model: AdapterConfig | None = None
    notify: list[AdapterConfig] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    dispatch: Policy = field(default_factory=Policy)
    detectors: DetectorsConfig = field(default_factory=DetectorsConfig)
    source_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source_path: str = "") -> Config:
        raw = expand(raw or {})
        if not isinstance(raw, dict):
            raise ConfigError("config: la raíz del YAML tiene que ser un mapa.")

        srv = raw.get("server") or {}
        server = ServerConfig(
            host=str(srv.get("host", "0.0.0.0")),  # noqa: S104
            port=int(srv.get("port", 8080)),
            api_token=str(srv.get("api_token", "") or ""),
            allow_insecure=bool(srv.get("allow_insecure", False)),
        )

        ag = raw.get("agent") or {}
        agent = AgentConfig(
            max_steps=int(ag.get("max_steps", 12)),
            max_seconds=parse_duration(
                ag.get("max_seconds", "180s"), where="agent.max_seconds").total_seconds(),
            temperature=float(ag.get("temperature", 0.2)),
            system_prompt=str(ag.get("system_prompt", "") or ""),
        )

        bud = raw.get("budget") or {}
        budget = Budget(
            max_series=int(bud.get("max_series", 500)),
            max_points=int(bud.get("max_points", 11_000)),
            max_range=parse_duration(bud.get("max_range", "31d"), where="budget.max_range"),
            timeout_s=parse_duration(
                bud.get("timeout", "30s"), where="budget.timeout").total_seconds(),
        )

        notify_raw = raw.get("notify") or []
        if not isinstance(notify_raw, list):
            raise ConfigError("notify: tiene que ser una lista de canales.")

        return cls(
            server=server,
            agent=agent,
            budget=budget,
            metrics=(AdapterConfig.parse(raw["metrics"], where="metrics")
                     if raw.get("metrics") else None),
            cost=(AdapterConfig.parse(raw["cost"], where="cost")
                  if raw.get("cost") else None),
            model=(AdapterConfig.parse(raw["model"], where="model")
                   if raw.get("model") else None),
            notify=[AdapterConfig.parse(c, where=f"notify[{i}]")
                    for i, c in enumerate(notify_raw)],
            dispatch=_parse_dispatch(raw.get("dispatch") or {}),
            detectors=_parse_detectors(raw.get("detectors") or {}),
            source_path=source_path,
        )

    def problems(self) -> list[str]:
        """Todo lo que impide arrancar, junto.

        Junto, no el primero: quien está configurando esto quiere arreglar los
        cuatro errores de una y volver a levantar, no descubrirlos de a uno con
        un reinicio entre cada par.
        """
        out: list[str] = []
        if self.metrics is None:
            out.append(
                "metrics: falta el bloque. Es el único puerto obligatorio — sin "
                "métricas el agente no tiene con qué trabajar.")
        if self.model is None:
            out.append(
                "model: falta el bloque. Apuntalo al endpoint compatible con "
                "OpenAI que ya tenga el cliente (vLLM, LiteLLM, MaaS, Azure).")
        if self.agent.max_steps < 1:
            out.append("agent.max_steps: tiene que ser al menos 1.")
        if self.budget.max_series < 1:
            out.append("budget.max_series: tiene que ser al menos 1.")

        cs = self.detectors.cost_spike
        if cs.enabled and self.cost is None:
            out.append(
                "detectors.cost_spike: está habilitado pero no hay bloque `cost`. "
                "Sin costos no hay nada que detectar.")
        if cs.threshold <= 1:
            out.append("detectors.cost_spike.threshold: tiene que ser mayor que 1 "
                       "(1.5 = un 50% por encima de la mediana).")
        if cs.window_days < cs.min_days:
            out.append("detectors.cost_spike.window_days: tiene que ser al menos min_days.")
        if self.dispatch.daily_cap < 1:
            out.append("dispatch.daily_cap: tiene que ser al menos 1.")

        vistos: set[str] = set()
        for i, canal in enumerate(self.notify):
            if canal.name in vistos:
                out.append(
                    f"notify[{i}].name: '{canal.name}' está repetido. Los nombres "
                    f"son la llave del ruteo por severidad; repetidos, uno gana en "
                    f"silencio y el otro nunca entrega.")
            vistos.add(canal.name)

        if not self.server.api_token and not self.server.allow_insecure:
            out.append(
                "server.api_token: vacío. El endpoint de chat y el de ingreso de "
                "alertas quedarían abiertos. Poné un token, o `allow_insecure: "
                "true` si esto es tu máquina.")
        return out

    def validate_or_die(self) -> None:
        problemas = self.problems()
        if not problemas:
            return
        detalle = "\n".join(f"  - {p}" for p in problemas)
        raise ConfigError(
            f"No se puede arrancar con esta configuración"
            f"{f' ({self.source_path})' if self.source_path else ''}:\n{detalle}")


def load(path: str | Path | None = None) -> Config:
    """Carga y valida. Levanta `ConfigError` con todo lo que esté mal."""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        raise ConfigError(
            f"No existe {p}. Copiá config/copilot.example.yaml y editalo, o "
            f"apuntá COPILOT_CONFIG a otro archivo.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} no es YAML válido: {e}") from e
    cfg = Config.from_dict(raw or {}, source_path=str(p))
    cfg.validate_or_die()
    return cfg
